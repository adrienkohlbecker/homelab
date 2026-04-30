#!/usr/bin/env -S uv run

import asyncio
import contextlib
import shlex
import signal
import sys
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
        super().__init__(
            f"Command failed with exit code {exitcode}: {shlex.join(cmd)}{suffix}"
        )


class IdempotenceFailedException(Exception):
    """Raised when re-running an ansible play reports changed tasks."""
    pass


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
    with path.open("w") as handle:
        _OUTPUT_LOG = handle
        try:
            yield
        finally:
            _OUTPUT_LOG = None


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


def _emit(text: str) -> None:
    """Write *text* verbatim to stdout and the active tee target, if any."""
    sys.stdout.write(text)
    sys.stdout.flush()
    if _OUTPUT_LOG is not None:
        _OUTPUT_LOG.write(text)
        _OUTPUT_LOG.flush()


def _write_line(line: str, color: str | None) -> None:
    """Echo a line to stdout (and the active tee target, if any), optionally colorized."""
    _emit(colorize(line, color) + "\n")


def print_cmd_line(cmd: list[str]) -> None:
    """Log the command being executed in a distinct color."""
    _write_line(f"$ {shlex.join(cmd)}", "cyan")


def print_line(line: str, stderr: bool = False) -> None:
    """Log a free-form message through the same path as subprocess output.

    Routes through _write_line so the active tee_output target captures it,
    mirroring print()'s behavior otherwise. Pass stderr=True to render the
    line with the red error highlight used for subprocess stderr.
    """
    _write_line(line, "red" if stderr else None)


async def read_and_write_stream(
    stream: asyncio.StreamReader | None,
    color: str | None,
    capture: list[str],
    *,
    quiet: bool = False,
) -> None:
    """Relay a process stream to stdout and the log, capturing each line.

    When *quiet* is True the line is captured but not echoed -- useful for
    probe-style commands whose JSON/structured output would drown the
    transcript.
    """
    if stream is None:
        return

    while True:
        line_bytes = await stream.readline()
        if not line_bytes:
            break

        line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
        capture.append(line)
        if not quiet:
            _write_line(line, color)


async def terminate_subprocess(
    proc: asyncio.subprocess.Process,
    *,
    grace_seconds: float = 0.0,
    initial_signal: int = signal.SIGKILL,
) -> None:
    """Stop *proc*, escalating to SIGKILL after *grace_seconds* if needed.

    Default (grace=0, signal=SIGKILL) is the immediate-kill-and-drain used
    when a caller's own coroutine has failed and just needs the child gone.
    Pass a non-zero grace and signal=SIGINT for graceful shutdown -- useful
    when the child runs its own cleanup (qemu/podman teardown, log drain,
    etc.) and SIGKILL would leak resources.
    """
    with contextlib.suppress(ProcessLookupError):
        proc.send_signal(initial_signal)
    if grace_seconds > 0:
        try:
            async with asyncio.timeout(grace_seconds):
                await proc.wait()
            return
        except TimeoutError:
            # asyncio.timeout converts the inner CancelledError into
            # TimeoutError; only here can we tell the wait actually timed
            # out and escalate.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
    await proc.wait()


async def run_command(cmd: list[str], check: bool = True, quiet: bool = False) -> CommandResult:
    """
    Execute a subprocess, stream its output live and colorized.

    Args:
        cmd: Command and arguments to execute.
        check: If True, raise CommandFailedException on non-zero exit.
        quiet: If True, capture output without echoing it -- for probe
            commands (podman inspect, lsof) whose output is parsed, not
            displayed. The cmd line itself is also not printed.

    Returns:
        CommandResult with the exit code and captured stdout/stderr lines.
    """
    if not quiet:
        print_cmd_line(cmd)

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

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
        # interleave. Acceptable here because callers (ansible-playbook, ssh,
        # podman) emit ~all output on one stream; for source-order fidelity
        # use stderr=asyncio.subprocess.STDOUT, which costs the per-stream
        # color tagging.
        async with asyncio.TaskGroup() as tg:
            tg.create_task(read_and_write_stream(process.stdout, None, stdout, quiet=quiet))
            tg.create_task(read_and_write_stream(process.stderr, "red", stderr, quiet=quiet))
        exitcode = await process.wait()
    except BaseException:
        # Any failure (cancellation, reader error, etc.) leaves the subprocess
        # behind unless we tear it down here.
        await terminate_subprocess(process)
        raise

    if check and exitcode != 0:
        raise CommandFailedException(cmd, exitcode, stderr)
    return CommandResult(exitcode=exitcode, stdout=stdout, stderr=stderr)
