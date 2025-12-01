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
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

UBUNTU_NAME = "jammy"
UBUNTU_VERSION = "22.04"
SSH_KEY = "packer/vagrant.key"
SSH_HOST = "127.0.0.1"

CONTAINER_ANSIBLE_ARGS = ["-e", '{"docker_test":true}', "-e", "@host_vars/box-podman.yml"]
QEMU_MACHINE_ARGS: Dict[str, Tuple[str, List[str], str]] = {
    "minimal": (
        "ubuntu",
        ["-e", '{"qemu_test":true,"qemu_test_minimal":true}', "-e", "@host_vars/box-qemu-minimal.yml"],
        "box",
    ),
    "box": (
        "vagrant",
        ["-e", '{"qemu_test":true,"qemu_test_minimal":false}', "-e", "@host_vars/box-qemu.yml"],
        "box",
    ),
    "lab": (
        "vagrant",
        ["-e", '{"qemu_test":true,"qemu_test_minimal":false}', "-e", "@host_vars/lab-qemu.yml"],
        "lab",
    ),
    "pug": (
        "vagrant",
        ["-e", '{"qemu_test":true,"qemu_test_minimal":false}', "-e", "@host_vars/pug-qemu.yml"],
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

env_ansible_args = []


def machine_env(machine: str) -> Dict[str, str]:
    """Return machine-specific environment overrides."""
    env: Dict[str, str] = {}
    if machine == "container":
        env["SSH_USER"] = "root"
        env["ANSIBLE_ARGS"] = shlex.join(CONTAINER_ANSIBLE_ARGS)
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
        env["ANSIBLE_ARGS"] = shlex.join(ansible_args)
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


def sleep_tick() -> None:
    sys.stdout.write(".")
    sys.stdout.flush()
    time.sleep(1)


def colorize_line(line: str) -> str:
    """
    Add ANSI color codes to a line based on content.

    Lines starting with '+' (bash -x) are dimmed.
    All other lines are highlighted as errors.
    """
    if line.startswith("+"):
        return f"\033[0;30m{line}\033[0m"
    else:
        return f"\033[0;41m{line}\033[0m"


async def process_line(
    line: str,
    stream_name: str,
    file_handle,
    file_lock: asyncio.Lock,
) -> None:
    output_line = colorize_line(line) if stream_name == "stderr" else line

    # Print immediately to stdout
    sys.stdout.write(output_line + "\n")
    sys.stdout.flush()

    # Write to log, keeping writes from both streams serialized.
    async with file_lock:
        file_handle.write(output_line + "\n")
        file_handle.flush()


async def read_and_write_stream(
    stream: asyncio.StreamReader | None,
    stream_name: str,
    file_handle,
    file_lock: asyncio.Lock,
) -> None:
    """
    Read a stream, echo it live, and write it to the log file with coloring.

    The lock keeps multi-stream writes atomic so lines don't interleave.
    """
    if stream == None:
        return

    while True:
        try:
            line_bytes = await stream.readline()
            if not line_bytes:
                break

            line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
            await process_line(line, stream_name, file_handle, file_lock)

        except Exception as e:
            print(f"Error reading {stream_name}: {e}", file=sys.stderr)
            break


async def run_command(cmd: List[str], output_file: str, env: Dict[str, str], check: bool = True) -> int:
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

        exitcode = await process.wait()
        if check and exitcode != 0:
            raise Exception("Command failed")
        return exitcode


def format_ssh_cmd(port: int, key: str, user: str, host: str, cmd: Optional[List[str]] = None) -> List[str]:
    parts = ["ssh", "-i", key, "-p", str(port), "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", f"{user}@{host}"]
    if cmd:
        parts.append(shlex.join(cmd))
    return parts


def format_ansible_cmd(port: int, key: str, user: str, host: str, ubuntu_mirror: str, ubuntu_mirror_security: str, ansible_args: str, cmd: Optional[List[str]] = None) -> List[str]:
    parts = [
        "ansible-playbook",
        "-e",
        f"ansible_ssh_port={port}",
        "-e",
        f"ansible_ssh_host={host}",
        "-e",
        f"ansible_ssh_user={user}",
        "-e",
        f"ansible_ssh_private_key_file={key}",
        "-e",
        f"ubuntu_mirror={ubuntu_mirror}",
        "-e",
        f"ubuntu_mirror_security={ubuntu_mirror_security}",
        "--inventory",
        "test/inventory.ini",
    ]
    if ansible_args:
        parts += shlex.split(ansible_args)
    if cmd:
        parts.append(shlex.join(cmd))
    return parts


def copy_files(workdir: str, role: str, inventory_host: str) -> None:

    Path("group_vars").copy_into(workdir)
    Path("host_vars").copy_into(workdir)
    Path("wireguard").copy_into(workdir)
    Path("roles").copy_into(workdir)

    with open(f"{workdir}/site.yml", "w") as site_yml:
        site_yml.write(
            f"""
- hosts: {inventory_host}
  roles:
    - {role}
"""
        )

    if Path(f"roles/{role}/tasks/_test.yml").exists():
        with open(f"{workdir}/_test.yml", "w") as test_yml:
            test_yml.write(
                f"""
- hosts: {inventory_host}
tasks:
    - import_role:
        name: {role}
        tasks_from: _test
    """
            )


def stop_machine(machine: str, podman: List[str], idfile: Path, timeout_proc: subprocess.Popen[bytes]):
    if machine == "container":
        # TODO ensure timeout_proc is also killed if podman stop fails for some reason
        # TODO print this command to stdout
        subprocess.run([*podman, "stop", "--ignore", "--time", "5", "--cidfile", str(idfile)], check=False)
    else:
        if timeout_proc and timeout_proc.poll() is None:
            try:
                timeout_proc.terminate()
            except Exception:
                pass


async def run_test(env: Dict[str, str], pass_args: List[str]) -> int:

    output_file = f"test/out/{env["ROLE"]}.{env["MACHINE"]}.ansi"
    Path(output_file).parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=env["IMAGEDIR"]) as workdir:

        env["WORKDIR"] = workdir
        inventory_host = env["INVENTORY_HOST"]
        role = env["ROLE"]
        machine = env["MACHINE"]
        imagedir = env["IMAGEDIR"]
        ubuntu_version = env["UBUNTU_VERSION"]
        ubuntu_name = env["UBUNTU_NAME"]
        idfile = Path(f"{workdir}/{env["IDFILE"]}")
        podman = shlex.split(env.get("PODMAN", "podman"))
        keep_vm = env["KEEP_VM"] == "1"

        copy_files(workdir, role, inventory_host)

        if machine == "container":
            exitcode = await run_command([*podman, "network", "inspect", "homelab_net"], output_file, env, check=False)
            if exitcode != 0:
                await run_command([*podman, "network", "create", "--subnet", "192.5.0.0/16", "homelab_net"], output_file, env)

        else:

            if keep_vm:
                qemu_display_args = ["-display", "vnc=:0,to=99", "-vga", "std", "-usb", "-device", "usb-tablet", "-k", "fr"]
            else:
                qemu_display_args = ["-display", "none"]

            if machine == "minimal":
                await run_command(["cloud-localds", f"{workdir}/seed.img", "test/minimal/user-data", "test/minimal/meta-data"], output_file, env)
                await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{imagedir}/ubuntu-{ubuntu_version}-minimal-cloudimg-amd64.img", "-F", "qcow2", f"{workdir}/disk.img", "20G"], output_file, env)

                qemu_drives = [
                    f"file={workdir}/disk.img,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                    f"file={workdir}/seed.img,if=virtio,format=raw",
                ]

            elif machine == "box":

                await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{imagedir}/{ubuntu_name}/ubuntu-{machine}/packer-ubuntu-1", "-F", "qcow2", f"{workdir}/packer-ubuntu-1"], output_file, env)
                await run_command(["cp", f"{imagedir}/{ubuntu_name}/ubuntu-{machine}/efivars.fd", f"{workdir}/efivars.fd"], output_file, env)

                qemu_drives = [
                    f"file={workdir}/packer-ubuntu-1,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                    "file=/usr/share/OVMF/OVMF_CODE.fd,if=pflash,unit=0,format=raw,readonly=on",
                    f"file={workdir}/efivars.fd,if=pflash,unit=1,format=raw",
                ]

            elif machine == "lab":

                await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{imagedir}/{ubuntu_name}/ubuntu-{machine}/packer-ubuntu-1", "-F", "qcow2", f"{workdir}/packer-ubuntu-1"], output_file, env)
                await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{imagedir}/{ubuntu_name}/ubuntu-{machine}/packer-ubuntu-2", "-F", "qcow2", f"{workdir}/packer-ubuntu-2"], output_file, env)
                await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{imagedir}/{ubuntu_name}/ubuntu-{machine}/packer-ubuntu-3", "-F", "qcow2", f"{workdir}/packer-ubuntu-3"], output_file, env)
                await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{imagedir}/{ubuntu_name}/ubuntu-{machine}/packer-ubuntu-4", "-F", "qcow2", f"{workdir}/packer-ubuntu-4"], output_file, env)
                await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{imagedir}/{ubuntu_name}/ubuntu-{machine}/packer-ubuntu-5", "-F", "qcow2", f"{workdir}/packer-ubuntu-5"], output_file, env)
                await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{imagedir}/{ubuntu_name}/ubuntu-{machine}/packer-ubuntu-6", "-F", "qcow2", f"{workdir}/packer-ubuntu-6"], output_file, env)
                await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{imagedir}/{ubuntu_name}/ubuntu-{machine}/packer-ubuntu-7", "-F", "qcow2", f"{workdir}/packer-ubuntu-7"], output_file, env)
                await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{imagedir}/{ubuntu_name}/ubuntu-{machine}/packer-ubuntu-8", "-F", "qcow2", f"{workdir}/packer-ubuntu-8"], output_file, env)
                await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{imagedir}/{ubuntu_name}/ubuntu-{machine}/packer-ubuntu-9", "-F", "qcow2", f"{workdir}/packer-ubuntu-9"], output_file, env)
                await run_command(["cp", f"{imagedir}/{ubuntu_name}/ubuntu-{machine}/efivars.fd", f"{workdir}/efivars.fd"], output_file, env)

                qemu_drives = [
                    f"file={workdir}/packer-ubuntu-1,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                    f"file={workdir}/packer-ubuntu-2,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                    f"file={workdir}/packer-ubuntu-3,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                    f"file={workdir}/packer-ubuntu-4,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                    f"file={workdir}/packer-ubuntu-5,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                    f"file={workdir}/packer-ubuntu-6,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                    f"file={workdir}/packer-ubuntu-7,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                    f"file={workdir}/packer-ubuntu-8,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                    f"file={workdir}/packer-ubuntu-9,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                    "file=/usr/share/OVMF/OVMF_CODE.fd,if=pflash,unit=0,format=raw,readonly=on",
                    f"file={workdir}/efivars.fd,if=pflash,unit=1,format=raw",
                ]

            elif machine == "pug":

                await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{imagedir}/{ubuntu_name}/ubuntu-{machine}/packer-ubuntu-1", "-F", "qcow2", f"{workdir}/packer-ubuntu-1"], output_file, env)
                await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{imagedir}/{ubuntu_name}/ubuntu-{machine}/packer-ubuntu-2", "-F", "qcow2", f"{workdir}/packer-ubuntu-2"], output_file, env)
                await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{imagedir}/{ubuntu_name}/ubuntu-{machine}/packer-ubuntu-3", "-F", "qcow2", f"{workdir}/packer-ubuntu-3"], output_file, env)
                await run_command(["cp", f"{imagedir}/{ubuntu_name}/ubuntu-{machine}/efivars.fd", f"{workdir}/efivars.fd"], output_file, env)

                qemu_drives = [
                    f"file={workdir}/packer-ubuntu-1,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                    f"file={workdir}/packer-ubuntu-2,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                    f"file={workdir}/packer-ubuntu-3,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                    "file=/usr/share/OVMF/OVMF_CODE.fd,if=pflash,unit=0,format=raw,readonly=on",
                    f"file={workdir}/efivars.fd,if=pflash,unit=1,format=raw",
                ]

            else:
                raise Exception(f"Unknown machine {machine}")

        if machine == "container":
            cmd = [
                "timeout",
                "--kill-after=10s",
                "10m",
                *podman,
                "run",
                "--rm",
                "--publish",
                "127.0.0.1::22",
                "--privileged",
                "--cidfile",
                str(idfile),
                "--network",
                "homelab_net",
                f"homelab:{ubuntu_name}",
            ]
        else:
            cmd = [
                "timeout",
                "--kill-after=10s",
                "10m",
                "qemu-system-x86_64",
                *[arg for d in qemu_drives for arg in ("--drive", d)],
                "-netdev",
                f"user,id=user.0,hostfwd=tcp:{env['SSH_HOST']}:0-:22",
                "-object",
                "rng-random,id=rng0,filename=/dev/urandom",
                "-device",
                "virtio-rng-pci,rng=rng0",
                "-machine",
                "type=q35,accel=kvm",
                "-smp",
                "8,sockets=8",
                "-name",
                "packer-ubuntu",
                "-m",
                "4096M",
                "-cpu",
                "host",
                *qemu_display_args,
                "-device",
                "virtio-net,netdev=user.0",
                "-pidfile",
                str(idfile),
            ]

        timeout_proc = subprocess.Popen(cmd)  # TODO print command to stdout
        port = 0

        try:

            while not idfile.exists():
                if timeout_proc.poll() is not None:
                    raise RuntimeError("Launching machine failed")
                sleep_tick()

            print("Booted")
            time.sleep(2)

            if machine == "container":
                cid = idfile.read_text().strip()
                if not cid:
                    raise RuntimeError("Missing container ID; podman run may have failed")

                addr = subprocess.check_output([*podman, "port", cid, "22"], text=True).strip()  # TODO: print this command
                if ":" not in addr:
                    raise RuntimeError(f"Unexpected podman port output: {addr}")

                port = int(addr.rsplit(":", 1)[-1])
            else:
                pid = idfile.read_text().strip()
                if not pid:
                    raise RuntimeError("Missing qemu PID; pidfile is empty")

                found = False
                for _ in range(10):
                    proc = subprocess.run(
                        ["lsof", "-i", "-P", "-p", pid],
                        capture_output=True,
                        text=True,
                    )  # TODO: pritn this command
                    output = proc.stdout or ""
                    for line in output.splitlines():
                        fields = line.split()
                        if fields[1] != pid or fields[7] != "TCP":
                            continue
                        match = re.search(r":(\d+)", line)
                        if not match:
                            continue
                        port_str = match.group(1)
                        if not port_str.startswith("59"):
                            found = True
                            port = int(port_str)
                            break

                    if found:
                        break

                    time.sleep(1)

                if not found:
                    raise RuntimeError("Unable to determine SSH port from qemu lsof output")

            env["PORT"] = str(port)
            host = env["SSH_HOST"]

            while True:  # TODO: this should stop after a while
                try:
                    with socket.create_connection((host, port), timeout=2) as s:
                        # Read the SSH banner which contains the version information
                        if s.recv(1024).decode().strip() != "":
                            break
                except OSError:
                    sleep_tick()

            print("SSH up")

            env["SSH_CMD"] = shlex.join(format_ssh_cmd(port, env["SSH_KEY"], env["SSH_USER"], env["SSH_HOST"]))
            env["ANSIBLE_PLAYBOOK"] = shlex.join(format_ansible_cmd(port, env["SSH_KEY"], env["SSH_USER"], env["SSH_HOST"], env["UBUNTU_MIRROR"], env["UBUNTU_MIRROR_SECURITY"], env["ANSIBLE_ARGS"]))

            await run_command(format_ssh_cmd(port, env["SSH_KEY"], env["SSH_USER"], env["SSH_HOST"], ["sudo", "truncate", "-s0", "/etc/apt/sources.list"]), output_file, env)
            await run_command(
                format_ssh_cmd(port, env["SSH_KEY"], env["SSH_USER"], env["SSH_HOST"], ["sudo", "bash", "-c", f'echo "deb {env["UBUNTU_MIRROR"]} {env["UBUNTU_NAME"]} main restricted universe multiverse" >> /etc/apt/sources.list']), output_file, env
            )
            await run_command(
                format_ssh_cmd(port, env["SSH_KEY"], env["SSH_USER"], env["SSH_HOST"], ["sudo", "bash", "-c", f'echo "deb {env["UBUNTU_MIRROR"]} {env["UBUNTU_NAME"]}-updates main restricted universe multiverse" >> /etc/apt/sources.list']),
                output_file,
                env,
            )
            await run_command(
                format_ssh_cmd(port, env["SSH_KEY"], env["SSH_USER"], env["SSH_HOST"], ["sudo", "bash", "-c", f'echo "deb {env["UBUNTU_MIRROR_SECURITY"]} {env["UBUNTU_NAME"]}-security main restricted universe multiverse" >> /etc/apt/sources.list']),
                output_file,
                env,
            )
            await run_command(
                format_ssh_cmd(port, env["SSH_KEY"], env["SSH_USER"], env["SSH_HOST"], ["sudo", "bash", "-c", f'echo "deb {env["UBUNTU_MIRROR"]} {env["UBUNTU_NAME"]}-backports main restricted universe multiverse" >> /etc/apt/sources.list']),
                output_file,
                env,
            )
            await run_command(format_ssh_cmd(port, env["SSH_KEY"], env["SSH_USER"], env["SSH_HOST"], ["sudo", "cat", "/etc/apt/sources.list"]), output_file, env)
            await run_command(format_ssh_cmd(port, env["SSH_KEY"], env["SSH_USER"], env["SSH_HOST"], ["sudo", "apt-get", "update"]), output_file, env)

            if machine == "minimal":
                # Fixes systemd-analyze validation error:
                # /lib/systemd/system/snapd.service:23: Unknown key name 'RestartMode' section 'Service', ignoring.
                await run_command(format_ssh_cmd(port, env["SSH_KEY"], env["SSH_USER"], env["SSH_HOST"], ["sudo", "apt-get", "purge", "--autoremove", "--yes", "snapd"]), output_file, env)

            env["ANSIBLE_DISPLAY_OK_HOSTS"] = "true"
            env["ANSIBLE_DISPLAY_SKIPPED_HOSTS"] = "true"

            cmd = ["test/testrole.sh", *pass_args]
            return await run_command(cmd, output_file, env)

        finally:

            if keep_vm and port != 0:
                ssh_cmd = shlex.join(format_ssh_cmd(port, env["SSH_KEY"], env["SSH_USER"], env["SSH_HOST"], []))
                if machine == "container":
                    stop_cmd = f"{shlex.join(podman)} stop --ignore --time 5 --cidfile {idfile}"
                else:
                    stop_cmd = f"kill {timeout_proc.pid}"

                print("Keeping VM around, ssh using:")
                print(f"> {ssh_cmd}")
                print("Then Ctrl+C or")
                print(f"> {stop_cmd}")

                signal.signal(signal.SIGINT, lambda *_: stop_machine(machine, podman, idfile, timeout_proc))
            else:
                stop_machine(machine, podman, idfile, timeout_proc)

            try:
                timeout_proc.wait()
            except Exception:
                pass


def main() -> int:
    """Main entry point."""

    parsed_args, pass_args = parse_args()
    env = build_env(parsed_args, pass_args)

    try:
        exit_code = asyncio.run(run_test(env, pass_args))

        if exit_code != 0:
            sys.stderr.write(f"\033[0;41m{env["ROLE"]}.{env["MACHINE"]} failed\033[0m\n")
            sys.stderr.flush()

        return exit_code

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
