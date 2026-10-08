import argparse
import atexit
import contextlib
import os
import queue
import shlex
import signal
import subprocess
import sys
import threading
import time
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import IO, NamedTuple, TextIO


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


# Templates expand `{line}` between an ANSI prefix and reset.
COLORS = {
    "red": "\033[0;41m{line}\033[0m",
    "cyan": "\033[0;36m{line}\033[0m",
    "green": "\033[0;32m{line}\033[0m",
}

# Compact console mode, set by testrole.py unless --verbose: only status lines
# (print_line) reach the terminal, each prefixed with this cell's tag, so cells
# interleaving under GNU parallel stay readable. Subprocess output and the
# commands themselves go to the run log alone.
_CONSOLE_TAG: str | None = None
# Distinct 256-colour foregrounds; a role always gets the same one.
_TAG_COLORS = (33, 39, 70, 75, 99, 135, 166, 172, 178, 204, 37, 141)
# How often a long phase reports it is still running in compact mode.
PHASE_HEARTBEAT_SECONDS = 300

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


def positive_seconds(value: str) -> int:
    """argparse type for a --timeout: 0 would disarm the session deadline."""
    seconds = int(value)
    if seconds < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1 second, got {seconds}")
    return seconds


def use_compact_console(role: str, cell: str) -> None:
    """Show only status lines on the terminal, tagged ``role cell`` in a colour
    derived from the role."""
    global _CONSOLE_TAG
    color = _TAG_COLORS[zlib.crc32(role.encode()) % len(_TAG_COLORS)]
    _CONSOLE_TAG = f"\033[38;5;{color}m{f'{role} {cell}':<28}\033[0m"


def _elapsed(start: float) -> str:
    minutes, seconds = divmod(int(time.monotonic() - start), 60)
    return f"{minutes}:{seconds:02d}"


@contextlib.contextmanager
def phase(name: str) -> Iterator[None]:
    """Report a test phase's start, result, and duration as status lines."""
    start = time.monotonic()
    print_line(f"▶ {name}")
    done = threading.Event()

    def heartbeat() -> None:
        while not done.wait(PHASE_HEARTBEAT_SECONDS):
            print_line(f"  {name} still running ({_elapsed(start)})")

    threading.Thread(target=heartbeat, name=f"phase-{name}", daemon=True).start()
    try:
        yield
    except BaseException:
        print_line(f"✗ {name} ({_elapsed(start)})", error=True)
        raise
    else:
        _write_line(f"✓ {name} ({_elapsed(start)})", "green", status=True)
    finally:
        done.set()


def print_log_tail(path: Path, lines: int = 40) -> None:
    """In compact mode, show a failed run's transcript tail and where it lives.

    Console only: the transcript already holds these lines.
    """
    if _CONSOLE_TAG is None:
        return
    with contextlib.suppress(OSError):  # an unreadable log still leaves its path below
        for line in path.read_text(errors="replace").splitlines()[-lines:]:
            _queue_stdout(f"{_CONSOLE_TAG} │ {line}\n")
    _queue_stdout(f"{_CONSOLE_TAG} log: {path}\n")


def sleep_tick() -> None:
    """Emit a single dot per second while a long-running task progresses."""
    _emit(".")
    time.sleep(1)


def colorize(line: str, color: str | None) -> str:
    """Return the line wrapped in ANSI codes when *color* is a known key."""
    template = COLORS.get(color) if color else None
    return template.format(line=line) if template else line


# Subprocess output is relayed line-by-line by the thread draining the child's
# pipe (run_command). A direct sys.stdout.write+flush there makes every line a
# blocking write(2): if whoever drains our stdout (a CI job-log pipe) stalls,
# the relay stops draining, the child blocks on its full pipe, and the cell
# hangs until its deadline. Hand the stdout half to a dedicated daemon thread
# so the relay only ever enqueues; the tee-file write stays inline (local disk,
# the authoritative transcript) so test/out/*.ansi stays complete even if
# stdout wedges and the daemon is killed at interpreter exit.
_STDOUT_QUEUE: queue.SimpleQueue[str | threading.Event] = queue.SimpleQueue()
_STDOUT_WRITER: threading.Thread | None = None
_STDOUT_WRITER_LOCK = threading.Lock()
# Serializes _emit across the stderr relay and phase heartbeat threads, so
# their lines never splice into one another in the transcript.
_EMIT_LOCK = threading.Lock()


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


def _queue_stdout(text: str) -> None:
    _ensure_stdout_writer()
    _STDOUT_QUEUE.put(text)


def _emit(text: str, *, status: bool = False) -> None:
    """Queue *text* for stdout and mirror it into the active tee target, if any.

    In compact console mode only *status* text reaches stdout, one tagged line
    at a time; everything still lands in the tee target.
    """
    with _EMIT_LOCK:
        if _CONSOLE_TAG is None:
            _queue_stdout(text)
        elif status:
            for line in text.splitlines():
                _queue_stdout(f"{_CONSOLE_TAG} {line}\n")
        if _OUTPUT_LOG is not None:
            _OUTPUT_LOG.write(text)
            _OUTPUT_LOG.flush()


def _write_line(line: str, color: str | None, *, status: bool = False) -> None:
    """Echo a line to stdout (and the active tee target, if any), optionally colorized."""
    _emit(colorize(line, color) + "\n", status=status)


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
    _write_line(line, "red" if error else None, status=True)


def _relay(stream: IO[str], color: str | None, capture: list[str]) -> None:
    """Relay a process stream to stdout and the log, capturing each line."""
    for line in stream:
        line = line.rstrip("\r\n")
        capture.append(line)
        _write_line(line, color)


# The signals that interrupt a cell: Ctrl-C, a stop request (GNU parallel's
# --termseq, a CI cancel), and the session deadline.
INTERRUPTS = (signal.SIGINT, signal.SIGTERM, signal.SIGALRM)
# How long an interrupted run_command waits for its stderr relay. A descendant
# of the killed child can hold the pipe open indefinitely; the relay is a
# daemon thread, so abandoning it costs nothing.
RELAY_DRAIN_SECONDS = 5


@contextlib.contextmanager
def interrupts_held(*, redeliver: bool = True) -> Iterator[None]:
    """Hold INTERRUPTS for the with-block, then redeliver them (or drop them).

    Closes the window between creating a resource and recording it where
    cleanup will find it: an interrupt arriving in between would otherwise
    leak the resource. Python-level handlers rather than a blocked signal
    mask, because children spawned inside the block would inherit the mask but
    get default handlers back at exec. Redelivery raises from the block's exit,
    so open it inside the try that owns the cleanup.
    """
    pending: list[int] = []
    previous = {sig: signal.signal(sig, lambda signum, _frame: pending.append(signum)) for sig in INTERRUPTS}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if redeliver:
            for signum in pending:
                signal.raise_signal(signum)


def run_command(cmd: list[str], check: bool = True, *, env: dict[str, str] | None = None) -> CommandResult:
    """Execute a subprocess, stream its output live and colorized.

    check raises CommandFailedException on a non-zero exit. env layers
    overrides on top of os.environ. The child is killed if anything (the
    session deadline, Ctrl-C, a failed stderr relay) interrupts the call.
    """
    print_cmd_line(cmd, env=env)

    stdout: list[str] = []
    stderr: list[str] = []
    relay_failures: list[Exception] = []
    process: subprocess.Popen[str] | None = None
    stderr_relay: threading.Thread | None = None

    def drain_stderr(child: subprocess.Popen[str]) -> None:
        assert child.stderr is not None
        try:
            _relay(child.stderr, "red", stderr)
        except Exception as exc:
            # Killing the child ends the main thread's stdout read too.
            relay_failures.append(exc)
            child.kill()

    try:
        with interrupts_held():
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, **env} if env is not None else None,
                text=True,
                errors="replace",
            )
            # stderr drains on its own thread so neither pipe can fill and
            # block the child. Cross-stream order is therefore not preserved;
            # callers (ansible-playbook, ssh) emit nearly everything on stdout.
            stderr_relay = threading.Thread(target=drain_stderr, args=(process,), daemon=True)
            stderr_relay.start()
        assert process.stdout is not None
        _relay(process.stdout, None, stdout)
        exitcode = process.wait()
        stderr_relay.join()
    except BaseException:
        if process is not None:
            process.kill()
            process.wait()
        if stderr_relay is not None:
            stderr_relay.join(RELAY_DRAIN_SECONDS)
        raise
    if relay_failures:
        raise relay_failures[0]

    if check and exitcode != 0:
        raise CommandFailedException(cmd, exitcode, stderr)
    return CommandResult(exitcode=exitcode, stdout=stdout)
