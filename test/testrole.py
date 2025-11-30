#!/usr/bin/env python3
"""
Wrapper script to run a test role with colorized stderr output.

This script is called by GNU parallel to execute individual role tests.
It captures output, colorizes stderr based on content, and writes to log files.
"""

import argparse
import asyncio
import os
import sys
from typing import List


def colorize_line(line: str) -> str:
    """
    Add ANSI color codes to a line based on content.

    Lines starting with '+' (bash -x) are dimmed.
    All other lines are highlighted as errors.
    """
    if line.startswith('+'):
        return f'\033[0;30m{line}\033[0m'
    else:
        return f'\033[0;41m{line}\033[0m'


async def read_and_write_stream(
    stream: asyncio.StreamReader,
    stream_name: str,
    file_handle,
    file_lock: asyncio.Lock,
) -> None:
    """
    Read a stream, echo it live, and write it to the log file with coloring.

    The lock keeps multi-stream writes atomic so lines don't interleave.
    """
    while True:
        try:
            line_bytes = await stream.readline()
            if not line_bytes:
                break

            line = line_bytes.decode('utf-8', errors='replace').rstrip('\n')
            output_line = colorize_line(line) if stream_name == "stderr" else line

            # Print immediately to appropriate stream
            if stream_name == 'stdout':
                sys.stdout.write(line + '\n')
                sys.stdout.flush()
            else:
                sys.stderr.write(output_line + "\n")
                sys.stderr.flush()

            # Write to log, keeping writes from both streams serialized.
            async with file_lock:
                file_handle.write(output_line + "\n")
                file_handle.flush()

        except Exception as e:
            print(f"Error reading {stream_name}: {e}", file=sys.stderr)
            break


async def run_command(cmd: List[str], output_file: str) -> int:
    """
    Run a command and handle its output streams concurrently.

    Args:
        cmd: Command and arguments to execute
        output_file: Path to write combined output

    Returns:
        Exit code of the command
    """
    # Start subprocess
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    file_lock = asyncio.Lock()
    with open(output_file, "w") as f:
        await asyncio.gather(
            read_and_write_stream(process.stdout, "stdout", f, file_lock),
            read_and_write_stream(process.stderr, "stderr", f, file_lock),
        )

    # Wait for process to complete
    exit_code = await process.wait()

    return exit_code


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Run a single role test", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "--machine",
        default="container",
        help="Machine profile to run against (container|minimal|box|lab|pug)",
    )
    parser.add_argument("role", help="Role name to test")

    parsed_args, passthrough_args = parser.parse_known_args()

    machine = parsed_args.machine
    role = parsed_args.role
    output_file = f"test/out/{role}.{machine}.ansi"

    # Build command to execute
    cmd = [
        "test/testrole.sh",
        "--machine",
        machine,
        role,
        *passthrough_args,
    ]

    try:
        # Run command using asyncio
        exit_code = asyncio.run(run_command(cmd, output_file))

        # Print error message if failed
        if exit_code != 0:
            sys.stderr.write(f'\033[0;41m{role}.{machine} failed\033[0m\n')
            sys.stderr.flush()

        return exit_code

    except FileNotFoundError:
        print(f"Error: Script not found: {cmd[0]}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
