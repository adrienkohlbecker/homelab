#!/usr/bin/env python3

import asyncio
from ctypes import ArgumentError
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
from typing import Dict, Iterable, List, Optional, Tuple

from utils import run_command, sleep_tick, print_cmd_line

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

SSH_WAIT_TIMEOUT = 120
IDFILE_TIMEOUT = 60


def ubuntu_mirrors() -> Tuple[str, str]:
    """Return archive and security mirrors for the current CPU architecture."""
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
    raise SystemExit("Unknown machine name")


class Machine:
    """Base runner that wraps a test target reachable over SSH and Ansible."""
    ssh_host: str
    ssh_port: int
    ssh_user: str
    ssh_key: str
    ansible_args: List[str]
    inventory_host: str
    idfile: str
    imagedir: str
    proc: Optional[subprocess.Popen[bytes]]
    workdir: tempfile.TemporaryDirectory[str]
    output_file: Path
    journal_file: Path
    keep_vm: bool
    role: str

    def __init__(
        self,
        ssh_port: int,
        ssh_user: str,
        ansible_args: List[str],
        inventory_host: str,
        idfile: str,
        imagedir: str,
        machine: str,
        role: str,
        keep_vm: bool,
    ):
        """Initialize a machine wrapper with SSH and Ansible settings."""
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
        self.proc = None
        self.workdir = tempfile.TemporaryDirectory(dir=self.imagedir)

    def format_ssh_cmd(self, *cmd: str) -> List[str]:
        """Return an ssh invocation pinned to this instance."""

        base = [
            "ssh",
            "-i",
            self.ssh_key,
            "-p",
            str(self.ssh_port),
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            f"{self.ssh_user}@{self.ssh_host}",
        ]
        return [*base, shlex.join(cmd)] if cmd else base

    def format_ansible_cmd(self, *cmd: str) -> List[str]:
        """Build an ansible-playbook command pinned to this machine's SSH details."""
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

    async def ssh_command(self, *cmd: str, check: bool = True, captured_lines: Optional[List[str]] = None) -> int:
        """Execute an SSH command and stream output into the role log."""

        return await run_command(
            self.format_ssh_cmd(*cmd),
            self.output_file,
            check=check,
            captured_lines=captured_lines,
        )

    async def ansible_command(self, *cmd: str, check: bool = True, captured_lines: Optional[List[str]] = None) -> int:
        """Execute ansible-playbook with machine-specific SSH overrides."""

        return await run_command(
            self.format_ansible_cmd(*cmd),
            self.output_file,
            check=check,
            captured_lines=captured_lines,
        )

    async def prepare(self) -> None:
        """Stage a temporary workdir with inventory snippets and optional role test hook."""

        Path("group_vars").copy_into(self.workdir.name)
        Path("host_vars").copy_into(self.workdir.name)
        Path("wireguard").copy_into(self.workdir.name)
        Path("roles").copy_into(self.workdir.name)

        site_yml = Path(f"{self.workdir.name}/site.yml")
        site_yml.write_text(
            f"""
- hosts: {self.inventory_host}
  roles:
    - {self.role}
"""
        )

        if Path(f"roles/{self.role}/tasks/_test.yml").exists():
            Path(f"{self.workdir.name}/_test.yml").write_text(
                f"""
- hosts: {self.inventory_host}
  tasks:
    - import_role:
        name: {self.role}
        tasks_from: _test
"""
            )

    def _boot_command(self) -> List[str]:
        raise NotImplementedError

    async def _find_ssh_port(self) -> None:
        raise NotImplementedError

    async def boot(self) -> None:
        """Start the VM/container under a timeout wrapper."""

        cmd = ["timeout", "--kill-after=10s", "10m", *self._boot_command()]

        with open(self.output_file, "w") as f:
            await print_cmd_line(cmd, f, asyncio.Lock())

        self.proc = subprocess.Popen(cmd)

    def ensure_booted(self) -> None:
        """Block until the hypervisor writes the PID/CID file or the launch fails."""

        deadline = time.monotonic() + IDFILE_TIMEOUT
        id_path = Path(f"{self.workdir.name}/{self.idfile}")

        while not id_path.exists():
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError("Launching machine failed")
            if time.monotonic() > deadline:
                raise TimeoutError(f"PID file {id_path} not created within {IDFILE_TIMEOUT}s")
            sleep_tick()

    async def ensure_ssh(self) -> None:
        """Resolve SSH port then wait for the daemon banner to appear."""

        await self._find_ssh_port()

        deadline = time.monotonic() + SSH_WAIT_TIMEOUT
        while True:
            try:
                with socket.create_connection((self.ssh_host, self.ssh_port), timeout=2) as s:
                    banner = s.recv(1024).decode().strip()
                    if banner:
                        return
            except OSError:
                pass

            if time.monotonic() > deadline:
                raise TimeoutError("SSH daemon did not become ready in time")

            sleep_tick()

    async def collect_journal(self) -> None:
        """Fetch systemd journal for debugging when a run fails."""

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
                with open(self.output_file, "w") as f:
                    await print_cmd_line(cmd, f, asyncio.Lock())
                    subprocess.run(cmd, stdout=handle, stderr=f, check=True)

            print(f"Systemd journal: {self.journal_file}")

        except subprocess.CalledProcessError as exc:
            print(f"Failed to collect journal: {exc}", file=sys.stderr)

    def stop(self) -> None:
        """Stop the VM/container, optionally leaving it running for manual inspection."""

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

        if self.proc:
            try:
                self.proc.wait()
            except Exception:
                pass

        self.workdir.cleanup()

    def _stop_machine(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def _stop_cmd(self) -> List[str]:
        if not self.proc:
            raise RuntimeError("Process not started; cannot stop")
        return ["kill", str(self.proc.pid)]


class QemuMachine(Machine):
    """Start disposable QEMU guests for role-level integration tests."""
    machine: str
    drives: List[str]

    def __init__(self, machine: str, role: str, keep_vm: bool):
        """QEMU-backed machine wrapper used by integration tests."""
        try:
            ssh_user, ansible_args, inventory_host = QEMU_MACHINE_ARGS[machine]
        except KeyError:
            raise AttributeError(f"Unknown machine: {machine}") from None

        self.machine = machine
        super().__init__(
            ssh_port=0,
            ssh_user=ssh_user,
            ansible_args=ansible_args,
            inventory_host=inventory_host,
            idfile="pid",
            imagedir="/mnt/qemu",
            machine=machine,
            role=role,
            keep_vm=keep_vm,
        )

    async def prepare(self) -> None:
        """Create overlay images and seed data required for the selected QEMU template."""

        await super().prepare()

        if self.machine == "minimal":
            await run_command(
                ["cloud-localds", f"{self.workdir.name}/seed.img", "test/minimal/user-data", "test/minimal/meta-data"],
                self.output_file,
            )
            await self._create_overlay(
                f"{self.imagedir}/ubuntu-{UBUNTU_VERSION}-minimal-cloudimg-amd64.img",
                f"{self.workdir.name}/disk.img",
                size="20G",
            )
            self.drives = [
                self._virtio_drive(f"{self.workdir.name}/disk.img"),
                f"file={self.workdir.name}/seed.img,if=virtio,format=raw",
            ]
            return

        overlay_counts = {"box": 1, "lab": 9, "pug": 3}
        disk_count = overlay_counts.get(self.machine)
        if disk_count is None:
            raise AttributeError(f"Unknown machine {self.machine}")

        await self._create_overlay_series(disk_count)
        await self._copy_efivars()
        self.drives = [*self._virtio_drive_series(disk_count), *self._uefi_drives()]

    async def _create_overlay(self, src: str, dest: str, size: Optional[str] = None) -> None:
        """Create a qcow2 overlay pointing at *src* with optional resize."""

        args = ["qemu-img", "create", "-f", "qcow2", "-b", src, "-F", "qcow2", dest]
        if size:
            args.append(size)
        await run_command(args, self.output_file)

    async def _copy_efivars(self) -> None:
        """Copy EFI vars for UEFI boots into the working directory."""

        await run_command(
            ["cp", f"{self.imagedir}/{UBUNTU_NAME}/ubuntu-{self.machine}/efivars.fd", f"{self.workdir.name}/efivars.fd"],
            self.output_file,
        )

    async def _create_overlay_series(self, count: int) -> None:
        """Create a numbered set of overlays (packer-ubuntu-1..count)."""

        for idx in range(1, count + 1):
            await self._create_overlay(
                f"{self.imagedir}/{UBUNTU_NAME}/ubuntu-{self.machine}/packer-ubuntu-{idx}",
                f"{self.workdir.name}/packer-ubuntu-{idx}",
            )

    def _virtio_drive(self, path: str) -> str:
        """Return a virtio drive string with sensible cache/discard flags."""

        return f"file={path},if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap"

    def _virtio_drive_series(self, count: int) -> Iterable[str]:
        return (self._virtio_drive(f"{self.workdir.name}/packer-ubuntu-{idx}") for idx in range(1, count + 1))

    def _uefi_drives(self) -> List[str]:
        return [
            "file=/usr/share/OVMF/OVMF_CODE.fd,if=pflash,unit=0,format=raw,readonly=on",
            f"file={self.workdir.name}/efivars.fd,if=pflash,unit=1,format=raw",
        ]

    def _boot_command(self) -> List[str]:
        """Assemble qemu-system-x86_64 command line for the prepared disks."""

        if self.keep_vm:
            display_args = ["-display", "vnc=:0,to=99", "-vga", "std", "-usb", "-device", "usb-tablet", "-k", "fr"]
        else:
            display_args = ["-display", "none"]

        return [
            "qemu-system-x86_64",
            *[arg for drive in self.drives for arg in ("--drive", drive)],
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
            f"{self.workdir.name}/{self.idfile}",
        ]

    async def _find_ssh_port(self) -> None:
        """Parse lsof output from the QEMU pidfile to extract forwarded SSH port."""
        pid = Path(f"{self.workdir.name}/{self.idfile}").read_text().strip()
        if not pid:
            raise RuntimeError("Missing qemu PID; pidfile is empty")

        for _ in range(10):
            lines: List[str] = []
            await run_command(["lsof", "-i", "-P", "-p", pid], self.output_file, captured_lines=lines)

            for line in lines:
                # Ignore unrelated descriptors and VNC forwards (59xx range).
                fields = line.split()
                if len(fields) < 8 or fields[1] != pid or fields[7] != "TCP":
                    continue

                match = re.search(r":(\d+)", line)
                if not match:
                    continue

                port_str = match.group(1)
                if port_str.startswith("59"):
                    continue

                self.ssh_port = int(port_str)
                return

            sleep_tick()

        raise RuntimeError("Unable to determine SSH port from qemu lsof output")


class PodmanMachine(Machine):
    """Start privileged Podman containers that mimic SSH hosts."""
    podman: List[str]

    def __init__(self, machine: str, role: str, keep_vm: bool):
        """Podman-backed machine wrapper used by integration tests."""
        system = platform.system()
        if system == "Darwin":
            imagedir = os.environ.get("TMPDIR", "/tmp")
            self.podman = ["podman"]
        elif system == "Linux":
            imagedir = "/mnt/qemu"
            self.podman = ["sudo", "podman"]
        else:
            raise AttributeError("Unknown operating system")

        super().__init__(
            ssh_port=0,
            ssh_user="root",
            ansible_args=CONTAINER_ANSIBLE_ARGS,
            inventory_host="box",
            idfile="cid",
            imagedir=imagedir,
            machine=machine,
            role=role,
            keep_vm=keep_vm,
        )

    async def prepare(self) -> None:
        """Create podman network if missing and stage working dir."""

        await super().prepare()

        exitcode = await run_command([*self.podman, "network", "inspect", "homelab_net"], self.output_file, check=False)
        if exitcode != 0:
            await run_command([*self.podman, "network", "create", "--subnet", "192.5.0.0/16", "homelab_net"], self.output_file)

    def _boot_command(self) -> List[str]:
        """Return the podman run command that exposes SSH on a random host port."""

        return [
            *self.podman,
            "run",
            "--rm",
            "--publish",
            "127.0.0.1::22",
            "--privileged",
            "--cidfile",
            f"{self.workdir.name}/{self.idfile}",
            "--network",
            "homelab_net",
            f"homelab:{UBUNTU_NAME}",
        ]

    async def _find_ssh_port(self) -> None:
        """Ask podman for the forwarded SSH port and store it on the instance."""

        cid = Path(f"{self.workdir.name}/{self.idfile}").read_text().strip()
        if not cid:
            raise RuntimeError("Missing container ID; podman run may have failed")

        lines = []
        await run_command([*self.podman, "port", cid, "22"], self.output_file, captured_lines=lines)

        addr = "\n".join(lines).strip()
        if ":" not in addr:
            raise RuntimeError(f"Unexpected podman port output: {addr}")

        self.ssh_port = int(addr.rsplit(":", 1)[-1])
