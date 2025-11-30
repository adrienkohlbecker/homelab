#!/usr/bin/env python3
"""
Configure and run a single role test with colored output.

This replaces the bash shim that previously set environment variables before
dispatching to test/testrole.sh. The heavy lifting still happens in that
shell script; this file handles argument parsing, environment setup, and
log streaming.
"""

import argparse
import asyncio
import os
import platform
import shlex
import sys
from pathlib import Path
from typing import Dict, List, Tuple

UBUNTU_NAME = "jammy"
UBUNTU_VERSION = "22.04"
SSH_KEY = "packer/vagrant.key"
SSH_HOST = "127.0.0.1"

CONTAINER_ANSIBLE_ARGS = '-e {"docker_test":true} -e @host_vars/box-podman.yml'
QEMU_MACHINE_ARGS: Dict[str, Tuple[str, str, str]] = {
    "minimal": (
        "ubuntu",
        '-e {"qemu_test":true,"qemu_test_minimal":true} -e @host_vars/box-qemu-minimal.yml',
        "box",
    ),
    "box": (
        "vagrant",
        '-e {"qemu_test":true,"qemu_test_minimal":false} -e @host_vars/box-qemu.yml',
        "box",
    ),
    "lab": (
        "vagrant",
        '-e {"qemu_test":true,"qemu_test_minimal":false} -e @host_vars/lab-qemu.yml',
        "lab",
    ),
    "pug": (
        "vagrant",
        '-e {"qemu_test":true,"qemu_test_minimal":false} -e @host_vars/pug-qemu.yml',
        "pug",
    ),
}


def parse_args() -> tuple[argparse.Namespace, List[str]]:
    """Parse CLI arguments; unknown args are forwarded to Ansible."""
    parser = argparse.ArgumentParser(
        description="Run a single role test",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--machine",
        default="container",
        choices=["container", "minimal", "box", "lab", "pug"],
        help="Machine profile to run against",
    )
    parser.add_argument(
        "--checkmode",
        action="store_true",
        help="Run ansible in check mode before the test",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the machine running after the test",
    )
    parser.add_argument(
        "--vnc",
        action="store_true",
        help="Enable VNC (only if using QEMU backend)",
    )
    parser.add_argument("role", help="Role name to test")

    args, pass_args = parser.parse_known_args()

    # argparse leaves the literal "--" in the remainder; drop it to mirror the
    # old shell parsing behavior.
    if pass_args and pass_args[0] == "--":
        pass_args = pass_args[1:]

    return args, pass_args


def ubuntu_mirrors() -> Tuple[str, str]:
    """Choose the Ubuntu mirror based on host architecture."""
    arch = platform.machine().lower()
    if arch in {"aarch64", "arm64"}:
        return (
            "http://ports.ubuntu.com/ubuntu-ports/",
            "http://ports.ubuntu.com/ubuntu-ports/",
        )
    if arch == "x86_64":
        return (
            "http://archive.ubuntu.com/ubuntu/",
            "http://security.ubuntu.com/ubuntu/",
        )
    sys.exit("Unknown machine name")


def apply_venv(env: Dict[str, str]) -> None:
    """Prefix PATH with the repo's virtualenv if it exists."""
    venv_bin = Path(".venv/bin")
    if not venv_bin.exists():
        return

    env["VIRTUAL_ENV"] = str(venv_bin.parent)
    env["PATH"] = str(venv_bin) + os.pathsep + env.get("PATH", "")


def machine_env(machine: str) -> Dict[str, str]:
    """Return machine-specific environment overrides."""
    env: Dict[str, str] = {}
    if machine == "container":
        env["SSH_USER"] = "root"
        env["ANSIBLE_ARGS"] = CONTAINER_ANSIBLE_ARGS
        env["IDFILE"] = "cid"
        env["INVENTORY_HOST"] = "box"

        system = platform.system()
        if system == "Darwin":
            env["IMAGEDIR"] = os.environ.get("TMPDIR", "/tmp")
            env["PODMAN"] = "podman"
        elif system == "Linux":
            env["IMAGEDIR"] = "/mnt/qemu"
            env["PODMAN"] = "sudo podman"
        else:
            sys.exit("Unknown operating system")
    else:
        env["IMAGEDIR"] = "/mnt/qemu"
        env["IDFILE"] = "pid"

        try:
            ssh_user, ansible_args, inventory_host = QEMU_MACHINE_ARGS[machine]
        except KeyError:
            sys.exit(f"Unknown machine: {machine}")

        env["SSH_USER"] = ssh_user
        env["ANSIBLE_ARGS"] = ansible_args
        env["INVENTORY_HOST"] = inventory_host

    return env


def build_env(
    args: argparse.Namespace,
    pass_args: List[str],
) -> Dict[str, str]:
    """Assemble the environment expected by testrole.sh."""
    ubuntu_mirror, ubuntu_mirror_security = ubuntu_mirrors()

    env: Dict[str, str] = os.environ.copy()
    env.update(
        {
            "SSH_KEY": SSH_KEY,
            "SSH_HOST": SSH_HOST,
            "UBUNTU_NAME": UBUNTU_NAME,
            "UBUNTU_VERSION": UBUNTU_VERSION,
            "PORT": "",
            "SSH_CMD": "",
            "RUN_CHECKMODE": "1" if args.checkmode else "0",
            "KEEP_VM": "1" if args.keep else "0",
            "QEMU_USE_VNC": "1" if args.vnc else "0",
            "MACHINE": args.machine,
            "UBUNTU_MIRROR": ubuntu_mirror,
            "UBUNTU_MIRROR_SECURITY": ubuntu_mirror_security,
            "ROLE": args.role,
            # Provide PASS_ARGS to the shell script as a convenience; the script
            # still consumes them positionally.
            "PASS_ARGS": " ".join(pass_args),
        }
    )
    env.update(machine_env(args.machine))
    apply_venv(env)

    return env


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


async def process_line(
    line: str,
    stream_name: str,
    file_handle,
    file_lock: asyncio.Lock,
) -> None:
    # Print immediately to appropriate stream
    output_line = colorize_line(line) if stream_name == "stderr" else line
    if stream_name == "stdout":
        sys.stdout.write(output_line + "\n")
        sys.stdout.flush()
    else:
        sys.stderr.write(output_line + "\n")
        sys.stderr.flush()

    # Write to log, keeping writes from both streams serialized.
    async with file_lock:
        file_handle.write(output_line + "\n")
        file_handle.flush()


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
            await process_line(line, stream_name, file_handle, file_lock)

        except Exception as e:
            print(f"Error reading {stream_name}: {e}", file=sys.stderr)
            break


async def run_command(cmd: List[str], output_file: str, env: Dict[str, str]) -> int:
    """Run a command and handle its output streams concurrently."""
    with open(output_file, "w") as f:
        file_lock = asyncio.Lock()

        cmd_line = shlex.join(cmd)
        colored_cmd = f"\033[0;36m$ {cmd_line}\033[0m"
        await process_line(colored_cmd, "stdout", f, file_lock)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        await asyncio.gather(
            read_and_write_stream(process.stdout, "stdout", f, file_lock),
            read_and_write_stream(process.stderr, "stderr", f, file_lock),
        )

    return await process.wait()


def main() -> int:
    """Main entry point."""
    parsed_args, pass_args = parse_args()
    env = build_env(parsed_args, pass_args)

    output_file = f"test/out/{parsed_args.role}.{parsed_args.machine}.ansi"
    Path(output_file).parent.mkdir(parents=True, exist_ok=True)

    cmd = ["test/testrole.sh", *pass_args]

    try:
        exit_code = asyncio.run(run_command(cmd, output_file, env))

        if exit_code != 0:
            sys.stderr.write(
                f"\033[0;41m{parsed_args.role}.{parsed_args.machine} failed\033[0m\n"
            )
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
