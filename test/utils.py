#!/usr/bin/env -S uv run

import asyncio
import contextlib
import shlex
import signal
import sys
from collections.abc import Iterator
from typing import NamedTuple


class CommandFailedException(Exception):
    """Raised when a subprocess exits with a non-zero status."""
    pass


class IdempotenceFailedException(Exception):
    """Raised when re-running an ansible play reports changed tasks."""
    pass


class CommandResult(NamedTuple):
    """Outcome of a subprocess invocation."""
    exitcode: int
    stdout: list[str]


# ANSI colors keyed by logical stream name for simple lookups.
STREAM_COLORS = {
    "stderr": "\033[0;41m{line}\033[0m",
    "cmd": "\033[0;36m{line}\033[0m",
}


async def sleep_tick() -> None:
    """Emit a single dot per second while a long-running task progresses."""
    sys.stdout.write(".")
    sys.stdout.flush()
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


def _colorize(line: str, stream_name: str) -> str:
    """Return the line wrapped in ANSI codes when a color is configured."""
    template = STREAM_COLORS.get(stream_name)
    return template.format(line=line) if template else line


def _write_line(line: str, stream_name: str) -> None:
    """Echo a line to stdout, colorized by stream name."""
    output_line = _colorize(line, stream_name)
    sys.stdout.write(output_line + "\n")
    sys.stdout.flush()


def print_cmd_line(cmd: list[str]) -> None:
    """Log the command being executed in a distinct color."""
    _write_line(f"$ {shlex.join(cmd)}", "cmd")


async def read_and_write_stream(stream: asyncio.StreamReader | None, stream_name: str, capture: list[str]) -> None:
    """Relay a process stream to stdout and the log, capturing each line."""
    if stream is None:
        return

    while True:
        line_bytes = await stream.readline()
        if not line_bytes:
            break

        line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
        capture.append(line)
        _write_line(line, stream_name)


async def run_command(cmd: list[str], check: bool = True) -> CommandResult:
    """
    Execute a subprocess, stream its output live and colorized.

    Args:
        cmd: Command and arguments to execute.
        check: If True, raise CommandFailedException on non-zero exit.

    Returns:
        CommandResult with the exit code and captured stdout lines.
    """
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
        async with asyncio.TaskGroup() as tg:
            tg.create_task(read_and_write_stream(process.stdout, "stdout", stdout))
            tg.create_task(read_and_write_stream(process.stderr, "stderr", stderr))
        exitcode = await process.wait()
    except BaseException:
        # Any failure (cancellation, reader error, etc.) leaves the subprocess
        # behind unless we tear it down here.
        try:
            process.kill()
        except ProcessLookupError:
            # Process already exited on its own; nothing to kill.
            pass
        await process.wait()
        raise

    if check and exitcode != 0:
        raise CommandFailedException(f"Command failed with exit code {exitcode}")
    return CommandResult(exitcode=exitcode, stdout=stdout)
