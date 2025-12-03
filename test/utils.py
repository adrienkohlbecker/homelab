#!/usr/bin/env python3

import asyncio
import shlex
import sys
import time
from io import TextIOWrapper
from pathlib import Path
from typing import List, Optional


class CommandFailedException(Exception):
    """Raised when a subprocess exits with a non-zero status."""
    pass


# ANSI colors keyed by logical stream name for simple lookups.
STREAM_COLORS = {
    "stderr": "\033[0;41m{line}\033[0m",
    "cmd": "\033[0;36m{line}\033[0m",
}


def sleep_tick() -> None:
    """Emit a single dot per second while a long-running task progresses."""
    sys.stdout.write(".")
    sys.stdout.flush()
    time.sleep(1)


def _colorize(line: str, stream_name: str) -> str:
    """Return the line wrapped in ANSI codes when a color is configured."""
    template = STREAM_COLORS.get(stream_name)
    return template.format(line=line) if template else line


async def _write_line(
    line: str,
    stream_name: str,
    file_handle: TextIOWrapper,
    file_lock: asyncio.Lock,
) -> None:
    """
    Echo a line to stdout and serialize writes to the shared log file.

    File writes are guarded by a lock because stdout and stderr are read
    concurrently and we do not want interleaved lines in the ANSI log.
    """
    output_line = _colorize(line, stream_name)
    sys.stdout.write(output_line + "\n")
    sys.stdout.flush()

    async with file_lock:
        file_handle.write(output_line + "\n")
        file_handle.flush()


async def print_cmd_line(cmd: List[str], file_handle: TextIOWrapper, file_lock: asyncio.Lock) -> None:
    """
    Log the command being executed in a distinct color.
    """
    await _write_line(f"$ {shlex.join(cmd)}", "cmd", file_handle, file_lock)


async def read_and_write_stream(
    stream: asyncio.StreamReader | None,
    stream_name: str,
    file_handle: TextIOWrapper,
    file_lock: asyncio.Lock,
    capture: Optional[List[str]] = None,
) -> None:
    """Relay a process stream to stdout and the log, optionally capturing it."""
    if stream is None:
        return

    while True:
        try:
            line_bytes = await stream.readline()
            if not line_bytes:
                break

            line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
            if capture is not None:
                capture.append(line)
            await _write_line(line, stream_name, file_handle, file_lock)

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # keep log noise explicit if decoding fails
            print(f"Error reading {stream_name}: {exc}", file=sys.stderr)
            break



async def run_command(
    cmd: List[str],
    output_file: Path,
    check: bool = True,
    captured_lines: Optional[List[str]] = None,
) -> int:
    """
    Execute a subprocess, stream its output live, and write a colorized log.

    Args:
        cmd: Command and arguments to execute.
        output_file: Path to write the combined, colorized log.
        check: If True, raise CommandFailedException on non-zero exit.
        captured_lines: Optional list populated with stdout lines (uncolored).

    Returns:
        The process exit code when check is False.
    """
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with open(output_file, "w") as f:
        file_lock = asyncio.Lock()
        await print_cmd_line(cmd, f, file_lock)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            # Read stdout/stderr concurrently while the process executes.
            await asyncio.gather(
                read_and_write_stream(process.stdout, "stdout", f, file_lock, captured_lines),
                read_and_write_stream(process.stderr, "stderr", f, file_lock),
            )
            exitcode = await process.wait()
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise

        if check and exitcode != 0:
            raise CommandFailedException(f"Command failed with exit code {exitcode}")
        return exitcode
