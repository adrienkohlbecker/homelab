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
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from black.output import out
from test.test_typing import Self

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


def sleep_tick() -> None:
    sys.stdout.write(".")
    sys.stdout.flush()
    time.sleep(1)


async def process_line(
    line: str,
    stream_name: str,
    file_handle,
    file_lock: asyncio.Lock,
) -> None:
    output_line = f"\033[0;41m{line}\033[0m" if stream_name == "stderr" else line

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
    capture: Optional[List[str]] = None,
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
            if capture is not None:
                capture.append(line)
            await process_line(line, stream_name, file_handle, file_lock)

        except Exception as e:
            print(f"Error reading {stream_name}: {e}", file=sys.stderr)
            break


async def run_command(
    cmd: List[str],
    output_file: Path,
    check: bool = True,
    captured_lines: Optional[List[str]] = None,
) -> int:
    """Run a command, stream output, and optionally return captured stdout."""
    with open(output_file, "w") as f:
        file_lock = asyncio.Lock()

        cmd_line = shlex.join(cmd)
        colored_cmd = f"\033[0;36m$ {cmd_line}\033[0m"
        await process_line(colored_cmd, "stdout", f, file_lock)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        await asyncio.gather(
            read_and_write_stream(process.stdout, "stdout", f, file_lock, captured_lines),
            read_and_write_stream(process.stderr, "stderr", f, file_lock),
        )

        exitcode = await process.wait()
        if check and exitcode != 0:
            raise Exception("Command failed")
        return exitcode


class Machine:
    ssh_host: str
    ssh_port: int
    ssh_user: str
    ssh_key: str
    ansible_args: List[str]
    inventory_host: str
    idfile: str
    imagedir: str
    proc: subprocess.Popen[bytes]
    workdir: Path
    output_file: Path
    journal_file: Path
    keep_vm: bool
    role: str

    def __init__(self, ssh_port: int, ssh_user: str, ansible_args: List[str], inventory_host: str, idfile: str, imagedir: str, machine: str, role: str, keep_vm: bool):
        self.ssh_host = SSH_HOST
        self.ssh_key = SSH_KEY
        self.ssh_port = ssh_port
        self.ssh_user = ssh_user
        self.ansible_args = ansible_args
        self.inventory_host = inventory_host
        self.idfile = idfile
        self.imagedir = imagedir
        self.keep_vm = keep_vm
        self.role = role

        out_dir = Path("test/out")
        out_dir.mkdir(parents=True, exist_ok=True)

        self.output_file = out_dir / f"{role}.{machine}.ansi"
        self.journal_file = out_dir / f"{role}.{machine}.journal.ansi"

    def format_ssh_cmd(self, *cmd: str) -> List[str]:
        parts = ["ssh", "-i", self.ssh_key, "-p", str(self.ssh_port), "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", f"{self.ssh_user}@{self.ssh_host}"]
        if cmd:
            parts.append(shlex.join(cmd))
        return parts

    def format_ansible_cmd(self, *cmd: str) -> List[str]:
        ubuntu_mirror, ubuntu_mirror_security = ubuntu_mirrors()
        parts = [
            "env",
            "ANSIBLE_DISPLAY_OK_HOSTS=true",
            "ANSIBLE_DISPLAY_SKIPPED_HOSTS=true",
            "ansible-playbook",
            "-e",
            f"ansible_ssh_port={self.ssh_port}",
            "-e",
            f"ansible_ssh_host={self.ssh_host}",
            "-e",
            f"ansible_ssh_user={self.ssh_user}",
            "-e",
            f"ansible_ssh_private_key_file={self.ssh_key}",
            "-e",
            f"ubuntu_mirror={ubuntu_mirror}",
            "-e",
            f"ubuntu_mirror_security={ubuntu_mirror_security}",
            "--inventory",
            "test/inventory.ini",
            *self.ansible_args,
        ]
        if cmd:
            parts += cmd
        return parts

    async def ssh_command(
        self,
        *cmd: str,
        check: bool = True,
        captured_lines: Optional[List[str]] = None,
    ) -> int:
        return await run_command(self.format_ssh_cmd(*cmd), self.output_file, check=check, captured_lines=captured_lines)

    async def ansible_command(
        self,
        *cmd: str,
        check: bool = True,
        captured_lines: Optional[List[str]] = None,
    ) -> int:
        return await run_command(self.format_ansible_cmd(*cmd), self.output_file, check=check, captured_lines=captured_lines)

    async def prepare(self) -> None:

        Path("group_vars").copy_into(self.workdir)
        Path("host_vars").copy_into(self.workdir)
        Path("wireguard").copy_into(self.workdir)
        Path("roles").copy_into(self.workdir)

        with open(f"{self.workdir}/site.yml", "w") as site_yml:
            site_yml.write(
                f"""
- hosts: {self.inventory_host}
  roles:
    - {self.role}
"""
            )

        if Path(f"roles/{self.role}/tasks/_test.yml").exists():
            with open(f"{self.workdir}/_test.yml", "w") as test_yml:
                test_yml.write(
                    f"""
- hosts: {self.inventory_host}
  tasks:
    - import_role:
        name: {self.role}
        tasks_from: _test
"""
                )

    def _boot_command(self) -> List[str]:
        raise Exception("Unimplemented")

    async def _find_ssh_port(self) -> None:
        raise Exception("Unimplemented")

    def _stop_machine(self) -> None:
        raise Exception("Unimplemented")

    def _stop_cmd(self) -> List[str]:
        raise Exception("Unimplemented")

    def boot(self) -> None:
        cmd = ["timeout", "--kill-after=10s", "10m", *self._boot_command()]

        self.proc = subprocess.Popen(cmd)  # TODO print command to stdout

    def ensure_booted(self):

        while not Path(f"{self.workdir}/{self.idfile}").exists():  # TODO this should fail after some time
            if self.proc.poll() is not None:
                raise RuntimeError("Launching machine failed")
            sleep_tick()

    async def ensure_ssh(self) -> None:

        await self._find_ssh_port()

        while True:  # TODO: this should stop after a while
            try:
                with socket.create_connection((self.ssh_host, self.ssh_port), timeout=2) as s:
                    # Read the SSH banner which contains the version information
                    if s.recv(1024).decode().strip() != "":
                        break
            except OSError:
                sleep_tick()

    def collect_journal(self) -> None:

        try:
            cmd = self.format_ssh_cmd(
                "env",
                "SYSTEMD_COLORS=true",
                "journalctl",
                "--pager-end",
                "--no-pager",
                "--priority",
                "info",
            )

            with self.journal_file.open("w") as handle:
                subprocess.run(cmd, stdout=handle, stderr=subprocess.STDOUT)

            print(f"Systemd journal: {self.journal_file}")

        except subprocess.CalledProcessError as exc:
            print(f"Failed to collect journal: {exc}", file=sys.stderr)

    def stop(self) -> None:

        if self.keep_vm and self.ssh_port != 0:
            ssh_cmd = shlex.join(self.format_ssh_cmd())
            stop_cmd = shlex.join(self._stop_cmd())

            print("Keeping VM around, ssh using:")
            print(f"> {ssh_cmd}")
            print("Then Ctrl+C or")
            print(f"> {stop_cmd}")

            signal.signal(signal.SIGINT, lambda *_: self._stop_machine())
        else:
            self._stop_machine()

        try:
            self.proc.wait()
        except Exception:
            pass


class QemuMachine(Machine):
    machine: str
    drives: List[str]

    def __init__(self, machine: str, role: str, keep_vm: bool):
        try:
            ssh_user, ansible_args, inventory_host = QEMU_MACHINE_ARGS[machine]
        except KeyError:
            sys.exit(f"Unknown machine: {machine}")

        self.machine = machine
        super().__init__(ssh_port=0, ssh_user=ssh_user, ansible_args=ansible_args, inventory_host=inventory_host, idfile="pid", imagedir="/mnt/qemu", machine=machine, role=role, keep_vm=keep_vm)

    async def prepare(self) -> None:

        await super().prepare()

        if self.machine == "minimal":
            await run_command(["cloud-localds", f"{self.workdir}/seed.img", "test/minimal/user-data", "test/minimal/meta-data"], self.output_file)
            await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{self.imagedir}/ubuntu-{UBUNTU_VERSION}-minimal-cloudimg-amd64.img", "-F", "qcow2", f"{self.workdir}/disk.img", "20G"], self.output_file)

            self.drives = [
                f"file={self.workdir}/disk.img,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                f"file={self.workdir}/seed.img,if=virtio,format=raw",
            ]

        elif self.machine == "box":

            await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{self.imagedir}/{UBUNTU_NAME}/ubuntu-{self.machine}/packer-ubuntu-1", "-F", "qcow2", f"{self.workdir}/packer-ubuntu-1"], self.output_file)
            await run_command(["cp", f"{self.imagedir}/{UBUNTU_NAME}/ubuntu-{self.machine}/efivars.fd", f"{self.workdir}/efivars.fd"], self.output_file)

            self.drives = [
                f"file={self.workdir}/packer-ubuntu-1,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                "file=/usr/share/OVMF/OVMF_CODE.fd,if=pflash,unit=0,format=raw,readonly=on",
                f"file={self.workdir}/efivars.fd,if=pflash,unit=1,format=raw",
            ]

        elif self.machine == "lab":

            await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{self.imagedir}/{UBUNTU_NAME}/ubuntu-{self.machine}/packer-ubuntu-1", "-F", "qcow2", f"{self.workdir}/packer-ubuntu-1"], self.output_file)
            await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{self.imagedir}/{UBUNTU_NAME}/ubuntu-{self.machine}/packer-ubuntu-2", "-F", "qcow2", f"{self.workdir}/packer-ubuntu-2"], self.output_file)
            await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{self.imagedir}/{UBUNTU_NAME}/ubuntu-{self.machine}/packer-ubuntu-3", "-F", "qcow2", f"{self.workdir}/packer-ubuntu-3"], self.output_file)
            await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{self.imagedir}/{UBUNTU_NAME}/ubuntu-{self.machine}/packer-ubuntu-4", "-F", "qcow2", f"{self.workdir}/packer-ubuntu-4"], self.output_file)
            await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{self.imagedir}/{UBUNTU_NAME}/ubuntu-{self.machine}/packer-ubuntu-5", "-F", "qcow2", f"{self.workdir}/packer-ubuntu-5"], self.output_file)
            await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{self.imagedir}/{UBUNTU_NAME}/ubuntu-{self.machine}/packer-ubuntu-6", "-F", "qcow2", f"{self.workdir}/packer-ubuntu-6"], self.output_file)
            await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{self.imagedir}/{UBUNTU_NAME}/ubuntu-{self.machine}/packer-ubuntu-7", "-F", "qcow2", f"{self.workdir}/packer-ubuntu-7"], self.output_file)
            await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{self.imagedir}/{UBUNTU_NAME}/ubuntu-{self.machine}/packer-ubuntu-8", "-F", "qcow2", f"{self.workdir}/packer-ubuntu-8"], self.output_file)
            await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{self.imagedir}/{UBUNTU_NAME}/ubuntu-{self.machine}/packer-ubuntu-9", "-F", "qcow2", f"{self.workdir}/packer-ubuntu-9"], self.output_file)
            await run_command(["cp", f"{self.imagedir}/{UBUNTU_NAME}/ubuntu-{self.machine}/efivars.fd", f"{self.workdir}/efivars.fd"], self.output_file)

            self.drives = [
                f"file={self.workdir}/packer-ubuntu-1,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                f"file={self.workdir}/packer-ubuntu-2,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                f"file={self.workdir}/packer-ubuntu-3,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                f"file={self.workdir}/packer-ubuntu-4,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                f"file={self.workdir}/packer-ubuntu-5,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                f"file={self.workdir}/packer-ubuntu-6,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                f"file={self.workdir}/packer-ubuntu-7,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                f"file={self.workdir}/packer-ubuntu-8,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                f"file={self.workdir}/packer-ubuntu-9,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                "file=/usr/share/OVMF/OVMF_CODE.fd,if=pflash,unit=0,format=raw,readonly=on",
                f"file={self.workdir}/efivars.fd,if=pflash,unit=1,format=raw",
            ]

        elif self.machine == "pug":

            await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{self.imagedir}/{UBUNTU_NAME}/ubuntu-{self.machine}/packer-ubuntu-1", "-F", "qcow2", f"{self.workdir}/packer-ubuntu-1"], self.output_file)
            await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{self.imagedir}/{UBUNTU_NAME}/ubuntu-{self.machine}/packer-ubuntu-2", "-F", "qcow2", f"{self.workdir}/packer-ubuntu-2"], self.output_file)
            await run_command(["qemu-img", "create", "-f", "qcow2", "-b", f"{self.imagedir}/{UBUNTU_NAME}/ubuntu-{self.machine}/packer-ubuntu-3", "-F", "qcow2", f"{self.workdir}/packer-ubuntu-3"], self.output_file)
            await run_command(["cp", f"{self.imagedir}/{UBUNTU_NAME}/ubuntu-{self.machine}/efivars.fd", f"{self.workdir}/efivars.fd"], self.output_file)

            self.drives = [
                f"file={self.workdir}/packer-ubuntu-1,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                f"file={self.workdir}/packer-ubuntu-2,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                f"file={self.workdir}/packer-ubuntu-3,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap",
                "file=/usr/share/OVMF/OVMF_CODE.fd,if=pflash,unit=0,format=raw,readonly=on",
                f"file={self.workdir}/efivars.fd,if=pflash,unit=1,format=raw",
            ]

        else:
            raise Exception(f"Unknown machine {self.machine}")

    def _boot_command(self) -> List[str]:

        if self.keep_vm:
            display_args = ["-display", "vnc=:0,to=99", "-vga", "std", "-usb", "-device", "usb-tablet", "-k", "fr"]
        else:
            display_args = ["-display", "none"]

        return [
            "qemu-system-x86_64",
            *[arg for d in self.drives for arg in ("--drive", d)],
            "-netdev",
            f"user,id=user.0,hostfwd=tcp:{self.ssh_host}:0-:22",
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
            *display_args,
            "-device",
            "virtio-net,netdev=user.0",
            "-pidfile",
            f"{self.workdir}/{self.idfile}",
        ]

    async def _find_ssh_port(self):
        pid = Path(f"{self.workdir}/{self.idfile}").read_text().strip()
        if not pid:
            raise RuntimeError("Missing qemu PID; pidfile is empty")

        found = False
        for _ in range(10):

            lines = []
            await run_command(["lsof", "-i", "-P", "-p", pid], self.output_file, captured_lines=lines)
            for line in lines:
                fields = line.split()
                if fields[1] != pid or fields[7] != "TCP":
                    continue
                match = re.search(r":(\d+)", line)
                if not match:
                    continue
                port_str = match.group(1)
                if not port_str.startswith("59"):
                    found = True
                    self.ssh_port = int(port_str)
                    break

            if found:
                break

            time.sleep(1)

        if not found:
            raise RuntimeError("Unable to determine SSH port from qemu lsof output")

    def _stop_machine(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def _stop_cmd(self) -> List[str]:
        return ["kill", str(self.proc.pid)]


class PodmanMachine(Machine):
    podman: List[str]

    def __init__(self, machine: str, role: str, keep_vm: bool):
        system = platform.system()
        if system == "Darwin":
            imagedir = os.environ.get("TMPDIR", "/tmp")
            self.podman = ["podman"]
        elif system == "Linux":
            imagedir = "/mnt/qemu"
            self.podman = ["sudo", "podman"]
        else:
            sys.exit("Unknown operating system")

        super().__init__(ssh_port=0, ssh_user="root", ansible_args=CONTAINER_ANSIBLE_ARGS, inventory_host="box", idfile="cid", imagedir=imagedir, machine=machine, role=role, keep_vm=keep_vm)

    async def prepare(self) -> None:

        await super().prepare()

        exitcode = await run_command([*self.podman, "network", "inspect", "homelab_net"], self.output_file, check=False)
        if exitcode != 0:
            await run_command([*self.podman, "network", "create", "--subnet", "192.5.0.0/16", "homelab_net"], self.output_file)

    def _boot_command(self) -> List[str]:
        return [
            *self.podman,
            "run",
            "--rm",
            "--publish",
            "127.0.0.1::22",
            "--privileged",
            "--cidfile",
            f"{self.workdir}/{self.idfile}",
            "--network",
            "homelab_net",
            f"homelab:{UBUNTU_NAME}",
        ]

    async def _find_ssh_port(self):

        cid = Path(f"{self.workdir}/{self.idfile}").read_text().strip()
        if not cid:
            raise RuntimeError("Missing container ID; podman run may have failed")

        lines = []
        await run_command([*self.podman, "port", cid, "22"], self.output_file, captured_lines=lines)

        addr = "\n".join(lines).strip()
        if ":" not in addr:
            raise RuntimeError(f"Unexpected podman port output: {addr}")

        self.ssh_port = int(addr.rsplit(":", 1)[-1])

    def _stop_machine(self) -> None:
        # TODO ensure timeout_proc is also killed if podman stop fails for some reason
        # TODO print this command to stdout
        subprocess.run([*self.podman, "stop", "--ignore", "--time", "5", "--cidfile", f"{self.workdir}/{self.idfile}"], check=False)

    def _stop_cmd(self) -> List[str]:
        return [*self.podman, "stop", "--ignore", "--time", "5", "--cidfile", f"{self.workdir}/{self.idfile}"]


async def run_test(parsed_args: argparse.Namespace, pass_args: List[str]) -> int:

    machine = parsed_args.machine
    role = parsed_args.role
    keep_vm = parsed_args.keep
    checkmode = parsed_args.checkmode

    if machine == "container":
        m = PodmanMachine(machine, role, keep_vm)
    else:
        m = QemuMachine(machine, role, keep_vm)

    with tempfile.TemporaryDirectory(dir=m.imagedir) as workdir:
        m.workdir = Path(workdir)

        await m.prepare()
        m.boot()

        try:

            m.ensure_booted()

            print("Booted")
            time.sleep(2)

            await m.ensure_ssh()

            print("SSH up")

            ubuntu_mirror, ubuntu_mirror_security = ubuntu_mirrors()

            await m.ssh_command("sudo", "truncate", "-s0", "/etc/apt/sources.list")
            await m.ssh_command("sudo", "bash", "-c", f'echo "deb {ubuntu_mirror} {UBUNTU_NAME} main restricted universe multiverse" >> /etc/apt/sources.list')
            await m.ssh_command(
                "sudo",
                "bash",
                "-c",
                f'echo "deb {ubuntu_mirror} {UBUNTU_NAME}-updates main restricted universe multiverse" >> /etc/apt/sources.list',
            )
            await m.ssh_command(
                "sudo",
                "bash",
                "-c",
                f'echo "deb {ubuntu_mirror_security} {UBUNTU_NAME}-security main restricted universe multiverse" >> /etc/apt/sources.list',
            )
            await m.ssh_command(
                "sudo",
                "bash",
                "-c",
                f'echo "deb {ubuntu_mirror} {UBUNTU_NAME}-backports main restricted universe multiverse" >> /etc/apt/sources.list',
            )
            await m.ssh_command("sudo", "cat", "/etc/apt/sources.list")
            await m.ssh_command("sudo", "apt-get", "update")

            if machine == "minimal":
                # Fixes systemd-analyze validation error:
                # /lib/systemd/system/snapd.service:23: Unknown key name 'RestartMode' section 'Service', ignoring.
                await m.ssh_command("sudo", "apt-get", "purge", "--autoremove", "--yes", "snapd")

            try:

                test_yml = f"{workdir}/_test.yml"
                if Path(test_yml).exists():
                    await m.ansible_command(test_yml)

                site_yml = f"{workdir}/site.yml"
                if checkmode:
                    list_tags: List[str] = []
                    await m.ansible_command(site_yml, "--list-tags", captured_lines=list_tags)

                    await m.ansible_command(site_yml, "--check", *pass_args)

                    for stage in ["_check_stage1", "_check_stage2", "_check_stage3", "_check_stage4"]:
                        if stage not in "\n".join(list_tags):
                            continue

                        await m.ansible_command(
                            site_yml,
                            "--tags",
                            stage,
                            *pass_args,
                        )
                        await m.ansible_command(
                            site_yml,
                            "--check",
                            *pass_args,
                        )

                await m.ansible_command(site_yml, *pass_args)
                return 0

            except Exception as exc:
                m.collect_journal()
                raise exc

        finally:

            m.stop()


def main() -> int:
    """Main entry point."""

    parsed_args, pass_args = parse_args()

    exit_code = asyncio.run(run_test(parsed_args, pass_args))

    if exit_code != 0:
        sys.stderr.write(f"\033[0;41mROLE.MACHINE failed\033[0m\n")  # TODO fix env
        sys.stderr.flush()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
