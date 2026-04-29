#!/usr/bin/env -S uv run

import asyncio
import shlex
import sys
from typing import List, Optional


class CommandFailedException(Exception):
    """Raised when a subprocess exits with a non-zero status."""
    pass


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


def _colorize(line: str, stream_name: str) -> str:
    """Return the line wrapped in ANSI codes when a color is configured."""
    template = STREAM_COLORS.get(stream_name)
    return template.format(line=line) if template else line


def _write_line(line: str, stream_name: str) -> None:
    """Echo a line to stdout, colorized by stream name."""
    output_line = _colorize(line, stream_name)
    sys.stdout.write(output_line + "\n")
    sys.stdout.flush()


def print_cmd_line(cmd: List[str]) -> None:
    """Log the command being executed in a distinct color."""
    _write_line(f"$ {shlex.join(cmd)}", "cmd")


async def read_and_write_stream(stream: asyncio.StreamReader | None, stream_name: str, capture: Optional[List[str]] = None) -> None:
    """Relay a process stream to stdout and the log, optionally capturing it."""
    if stream is None:
        return

    while True:
        line_bytes = await stream.readline()
        if not line_bytes:
            break

        line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
        if capture is not None:
            capture.append(line)
        _write_line(line, stream_name)


async def run_command(
    cmd: List[str],
    check: bool = True,
    captured_lines: Optional[List[str]] = None,
) -> int:
    """
    Execute a subprocess, stream its output live and colorized.

    Args:
        cmd: Command and arguments to execute.
        check: If True, raise CommandFailedException on non-zero exit.
        captured_lines: Optional list populated with stdout lines (uncolored).

    Returns:
        The process exit code when check is False.
    """
    print_cmd_line(cmd)

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        # Read stdout/stderr concurrently while the process executes.
        await asyncio.gather(
            read_and_write_stream(process.stdout, "stdout", captured_lines),
            read_and_write_stream(process.stderr, "stderr"),
        )
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
    return exitcode
