import asyncio
import atexit
import contextlib
import os
import queue
import shlex
import signal
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple, TextIO


class CommandFailedException(Exception):
    """Raised when a subprocess exits with a non-zero status."""

    def __init__(self, cmd: list[str], exitcode: int, stderr: list[str]) -> None:
        self.cmd = cmd
        self.exitcode = exitcode
        self.stderr = stderr
        tail = "\n".join(stderr[-20:])
        suffix = f"\n--- stderr tail ---\n{tail}" if tail else ""
        super().__init__(f"Command failed with exit code {exitcode}: {shlex.join(cmd)}{suffix}")


class IdempotenceFailedException(Exception):
    """Raised when re-running an ansible play reports changed tasks."""


class CheckFailedException(Exception):
    """Raised when a harness check on the converged guest fails."""


class CommandResult(NamedTuple):
    """Outcome of a subprocess invocation."""

    exitcode: int
    stdout: list[str]
    stderr: list[str]


# Templates expand `{line}` between an ANSI prefix and reset.
COLORS = {
    "red": "\033[0;41m{line}\033[0m",
    "cyan": "\033[0;36m{line}\033[0m",
}

# Optional file that mirrors every line written via _write_line / print_cmd_line.
# Set with tee_output() so callers can keep a transcript of a run alongside the
# systemd journal in test/out/.
_OUTPUT_LOG: TextIO | None = None


@contextlib.contextmanager
def tee_output(path: Path) -> Iterator[None]:
    """Mirror every _write_line / print_cmd_line call into *path* for the duration of the with-block."""
    global _OUTPUT_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = _OUTPUT_LOG
    with path.open("w") as handle:
        _OUTPUT_LOG = handle
        try:
            yield
        finally:
            _OUTPUT_LOG = previous


async def sleep_tick() -> None:
    """Emit a single dot per second while a long-running task progresses."""
    _emit(".")
    await asyncio.sleep(1)


@contextlib.contextmanager
def cancel_on_signal(task: asyncio.Task[object]) -> Iterator[None]:
    """Cancel *task* on SIGINT/SIGTERM for the duration of the with-block."""
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM)
    for sig in signals:
        loop.add_signal_handler(sig, task.cancel)
    try:
        yield
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)


def colorize(line: str, color: str | None) -> str:
    """Return the line wrapped in ANSI codes when *color* is a known key."""
    template = COLORS.get(color) if color else None
    return template.format(line=line) if template else line


# Subprocess output is relayed line-by-line from the asyncio event-loop thread
# (read_and_write_stream). A direct sys.stdout.write+flush there makes every
# line a blocking write(2) on the loop thread: if whoever drains our stdout (a
# CI job-log pipe) stalls, that write blocks the entire event loop -- and a
# blocked loop runs no timers, so run_test's asyncio.timeout deadline silently
# stops being enforced and a stall rides the outer CI job timeout instead. Hand
# the stdout half to a dedicated daemon thread so the loop only ever enqueues;
# the tee-file write stays inline (local disk, the authoritative transcript) so
# test/out/*.ansi stays complete even if stdout wedges and the daemon is killed
# at interpreter exit.
_STDOUT_QUEUE: queue.SimpleQueue[str | threading.Event] = queue.SimpleQueue()
_STDOUT_WRITER: threading.Thread | None = None
_STDOUT_WRITER_LOCK = threading.Lock()


def _stdout_writer_loop() -> None:
    while True:
        item = _STDOUT_QUEUE.get()
        if isinstance(item, threading.Event):
            # Drain barrier (see _drain_stdout): everything queued ahead of it
            # has been written, so release the waiter.
            item.set()
            continue
        try:
            sys.stdout.write(item)
            sys.stdout.flush()
        except BrokenPipeError, ValueError:
            # Consumer closed the pipe, or stdout was closed during shutdown:
            # nothing to write to. Keep draining so producers never block.
            pass


def _ensure_stdout_writer() -> None:
    global _STDOUT_WRITER
    if _STDOUT_WRITER is not None:
        return
    with _STDOUT_WRITER_LOCK:
        if _STDOUT_WRITER is None:
            _STDOUT_WRITER = threading.Thread(target=_stdout_writer_loop, name="harness-stdout-writer", daemon=True)
            _STDOUT_WRITER.start()


@atexit.register
def _drain_stdout(timeout: float = 2.0) -> None:
    """Flush queued stdout on a clean exit, bounded so a wedged pipe can't hang.

    The writer is a daemon thread, so the interpreter won't wait on it; push a
    barrier and wait briefly for the queue ahead of it to drain so a normal run
    keeps its tail output. A stuck stdout just times out here and the daemon
    dies with the interpreter.
    """
    if _STDOUT_WRITER is None:
        return
    barrier = threading.Event()
    _STDOUT_QUEUE.put(barrier)
    barrier.wait(timeout)


def _emit(text: str) -> None:
    """Queue *text* for stdout and mirror it into the active tee target, if any."""
    _ensure_stdout_writer()
    _STDOUT_QUEUE.put(text)
    if _OUTPUT_LOG is not None:
        _OUTPUT_LOG.write(text)
        _OUTPUT_LOG.flush()


def _write_line(line: str, color: str | None) -> None:
    """Echo a line to stdout (and the active tee target, if any), optionally colorized."""
    _emit(colorize(line, color) + "\n")


def print_cmd_line(cmd: list[str], env: dict[str, str] | None = None) -> None:
    """Log the command being executed in a distinct color.

    When *env* is supplied, render an `env K=V K=V ... cmd ...` prefix so the
    printed line stays copy-pasteable.
    """
    if env:
        env_parts = [f"{k}={shlex.quote(v)}" for k, v in env.items()]
        _write_line(f"$ env {' '.join(env_parts)} {shlex.join(cmd)}", "cyan")
    else:
        _write_line(f"$ {shlex.join(cmd)}", "cyan")


def print_line(line: str, error: bool = False) -> None:
    """Log a free-form message through the same path as subprocess output.

    Routes through _write_line so the active tee_output target captures it,
    mirroring print()'s behavior otherwise. Pass error=True to render the
    line with the red highlight used for subprocess stderr.
    """
    _write_line(line, "red" if error else None)


async def read_and_write_stream(stream: asyncio.StreamReader, color: str | None, capture: list[str]) -> None:
    """Relay a process stream to stdout and the log, capturing each line."""
    while True:
        line_bytes = await stream.readline()
        if not line_bytes:
            break

        line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
        capture.append(line)
        _write_line(line, color)


async def terminate_pid(pid: int, *, grace_seconds: float) -> None:
    """SIGTERM *pid*, escalating to SIGKILL after *grace_seconds* if needed.

    The pid-based counterpart to terminate_subprocess: used when the parent
    has only the child's PID (e.g. read out of a hypervisor pidfile), not
    a Popen handle. Uses kill(pid, 0) to detect exit; tolerant of the
    process having already gone.
    """
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)

    deadline = asyncio.get_running_loop().time() + grace_seconds
    while asyncio.get_running_loop().time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.2)

    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


async def terminate_subprocess(proc: asyncio.subprocess.Process, *, grace_seconds: float = 0.0) -> None:
    """Stop *proc*: SIGINT first when *grace_seconds* > 0, then SIGKILL.

    The default kills and drains immediately, for a caller whose own
    coroutine failed and just needs the child gone. A grace lets a child
    that runs its own teardown (testrole.py stopping its qemu) finish it.
    """
    if grace_seconds > 0:
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(signal.SIGINT)
        # The child outlived its grace when the bounded wait times out.
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(grace_seconds):
                await proc.wait()
            return
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    await proc.wait()


async def run_command(cmd: list[str], check: bool = True, *, env: dict[str, str] | None = None) -> CommandResult:
    """Execute a subprocess, stream its output live and colorized.

    check raises CommandFailedException on a non-zero exit. env layers
    overrides on top of os.environ. The child is killed if the call is
    cancelled or a reader fails.
    """
    print_cmd_line(cmd, env=env)

    subprocess_env: dict[str, str] | None = None
    if env is not None:
        subprocess_env = {**os.environ, **env}

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=subprocess_env,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    stdout: list[str] = []
    stderr: list[str] = []
    try:
        # Read stdout/stderr concurrently while the process executes. Use a
        # TaskGroup so a failure in either reader cancels the other and any
        # additional errors aggregate into an ExceptionGroup instead of being
        # silently dropped (as asyncio.gather would).
        # Ordering: lines within stdout (and within stderr) are FIFO, but
        # cross-stream order is NOT preserved -- the two pipes are independent
        # kernel objects and which reader is scheduled first decides the
        # interleave. Acceptable here because callers (ansible-playbook, ssh)
        # emit ~all output on one stream; for source-order fidelity
        # use stderr=asyncio.subprocess.STDOUT, which costs the per-stream
        # color tagging.
        async with asyncio.TaskGroup() as tg:
            tg.create_task(read_and_write_stream(process.stdout, None, stdout))
            tg.create_task(read_and_write_stream(process.stderr, "red", stderr))
        exitcode = await process.wait()
    except BaseException:
        # Any failure (cancellation, reader error, etc.) leaves the subprocess
        # behind unless we tear it down here.
        await terminate_subprocess(process)
        raise

    if check and exitcode != 0:
        raise CommandFailedException(cmd, exitcode, stderr)
    return CommandResult(exitcode=exitcode, stdout=stdout, stderr=stderr)
