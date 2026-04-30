#!/usr/bin/env -S uv run

import asyncio
import collections
import contextlib
import dataclasses
import fcntl
import os
import platform
import shlex
import signal
import tempfile
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Self

from utils import (
    CommandResult,
    IdempotenceFailedException,
    print_cmd_line,
    print_line,
    read_and_write_stream,
    run_command,
    sleep_tick,
    terminate_subprocess,
)

OUT_DIR = Path("test/out")
UBUNTU_RELEASES: dict[str, str] = {
    "jammy": "22.04",
    "noble": "24.04",
}
DEFAULT_UBUNTU = "jammy"
SSH_KEY = "packer/vagrant.key"
SSH_HOST = "127.0.0.1"
MACHINE_TIMEOUT = "900"  # 15 minutes; passed as a string to coreutils `timeout` and `podman --timeout`.

CONTAINER_ANSIBLE_ARGS = ["-e", '{"docker_test":true}', "-e", "@host_vars/box-podman.yml"]
QEMU_MACHINE_ARGS: dict[str, tuple[str, list[str], str]] = {
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
MACHINE_CHOICES: tuple[str, ...] = ("container", *QEMU_MACHINE_ARGS)

SSH_WAIT_TIMEOUT = 120
IDFILE_TIMEOUT = 60

PODMAN_NETWORK = "homelab_net"
PODMAN_NETWORK_SUBNET = "192.5.0.0/16"
PODMAN_NETWORK_LOCK = OUT_DIR / "podman_network.lock"

MEMORY_TSV = OUT_DIR / "memory.tsv"
MEMORY_TSV_LOCK = OUT_DIR / "memory.tsv.lock"
MEMORY_TSV_HEADER = "Role\tUbuntu\tMachine\tPeakKB"


@contextlib.contextmanager
def _memory_tsv_lock() -> Iterator[None]:
    """Cross-process exclusive lock guarding memory.tsv read-modify-write."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with MEMORY_TSV_LOCK.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
        # flock is released when the fd closes on with-block exit


def _read_memory_rows() -> dict[tuple[str, str, str], int]:
    if not MEMORY_TSV.exists():
        return {}
    rows: dict[tuple[str, str, str], int] = {}
    for line in MEMORY_TSV.read_text().splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) == 4:
            rows[(parts[0], parts[1], parts[2])] = int(parts[3])
    return rows


def _write_memory_rows(rows: dict[tuple[str, str, str], int]) -> None:
    lines = [MEMORY_TSV_HEADER]
    for key in sorted(rows):
        lines.append(f"{key[0]}\t{key[1]}\t{key[2]}\t{rows[key]}")
    # Write to a temp sibling and rename so a concurrent reader either sees
    # the prior file or the new one, never a half-written file.
    tmp = MEMORY_TSV.with_suffix(MEMORY_TSV.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(MEMORY_TSV)


def upsert_memory_row(role: str, ubuntu: str, machine: str, peak_kb: int) -> None:
    """Insert/update one row in memory.tsv, safe under concurrent writers.

    Multiple parallel testrole.py workers call this when their QEMU run
    completes; flock serialises the read-modify-write so updates aren't lost.
    """
    with _memory_tsv_lock():
        rows = _read_memory_rows()
        rows[(role, ubuntu, machine)] = peak_kb
        _write_memory_rows(rows)




async def ensure_podman_network() -> None:
    """Create the shared podman network if it doesn't exist.

    Safe under concurrent callers: a flock serialises the inspect-then-create
    so parallel PodmanMachine.prepare() calls don't race and lose with
    "network already exists".
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with PODMAN_NETWORK_LOCK.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = await run_command(["podman", "network", "inspect", PODMAN_NETWORK], check=False)
        if result.exitcode == 0:
            return
        await run_command(
            ["podman", "network", "create", "--subnet", PODMAN_NETWORK_SUBNET, PODMAN_NETWORK],
        )


def ubuntu_mirrors() -> tuple[str, str]:
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


@dataclasses.dataclass
class Machine:
    """Base runner that wraps a test target reachable over SSH and Ansible."""

    ssh_port: int
    ssh_user: str
    ansible_args: list[str]
    inventory_host: str
    idfile: str
    imagedir: str
    machine: str
    role: str
    keep_vm: bool
    ubuntu_name: str

    ssh_host: str = dataclasses.field(default=SSH_HOST, init=False)
    ssh_key: str = dataclasses.field(default=SSH_KEY, init=False)
    proc: asyncio.subprocess.Process | None = dataclasses.field(default=None, init=False)
    output_file: Path = dataclasses.field(init=False)
    journal_file: Path = dataclasses.field(init=False)
    boot_file: Path = dataclasses.field(init=False)
    workdir: tempfile.TemporaryDirectory[str] = dataclasses.field(init=False)
    peak_rss_kb: int = dataclasses.field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.ubuntu_name not in UBUNTU_RELEASES:
            raise ValueError(
                f"Unknown Ubuntu release '{self.ubuntu_name}'; known: {sorted(UBUNTU_RELEASES)}"
            )
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        prefix = f"{self.machine}.{self.ubuntu_name}.{self.role}"
        self.output_file = OUT_DIR / f"{prefix}.output.ansi"
        self.journal_file = OUT_DIR / f"{prefix}.journal.ansi"
        self.boot_file = OUT_DIR / f"{prefix}.boot.ansi"
        # Drop any stale artifacts from a previous run before the new run
        # starts writing. tee_output and boot() will recreate fresh files
        # immediately after this; cleaning here also covers cases where a
        # previous run was killed before either writer ran.
        self.cleanup_logs()
        self.workdir = tempfile.TemporaryDirectory(dir=self.imagedir)

    @property
    def ubuntu_version(self) -> str:
        """Numeric version (e.g. "22.04") for the configured release."""
        return UBUNTU_RELEASES[self.ubuntu_name]

    def format_ssh_cmd(self, *cmd: str) -> list[str]:
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
            "-o",
            "ConnectTimeout=10",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "BatchMode=yes",
            f"{self.ssh_user}@{self.ssh_host}",
        ]
        return [*base, shlex.join(cmd)] if cmd else base

    def format_ansible_cmd(self, *cmd: str) -> list[str]:
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

    async def ssh_command(self, *cmd: str, check: bool = True) -> CommandResult:
        """Execute an SSH command and stream output into the role log."""

        return await run_command(self.format_ssh_cmd(*cmd), check=check)

    async def ansible_command(self, *cmd: str, check: bool = True) -> CommandResult:
        """Execute ansible-playbook with machine-specific SSH overrides."""

        return await run_command(self.format_ansible_cmd(*cmd), check=check)

    async def prepare(self) -> None:
        """Stage a temporary workdir with inventory snippets and optional role test hooks."""

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

        # Per-role hook playbooks. _setup runs before the role apply; _verify
        # runs after. _test is the legacy name for setup-style hooks; many
        # roles still use it, so keep both working.
        for hook in ("_test", "_setup", "_verify"):
            if Path(f"roles/{self.role}/tasks/{hook}.yml").exists():
                Path(f"{self.workdir.name}/{hook}.yml").write_text(
                    f"""
- hosts: {self.inventory_host}
  tasks:
    - import_role:
        name: {self.role}
        tasks_from: {hook}
"""
                )

    def _boot_command(self) -> list[str]:
        raise NotImplementedError

    async def _find_ssh_port(self) -> None:
        raise NotImplementedError

    async def _sample_peak_rss(self, pid: int) -> None:
        status_path = Path(f"/proc/{pid}/status")
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                try:
                    for line in status_path.read_text().splitlines():
                        if line.startswith("VmRSS:"):
                            self.peak_rss_kb = max(self.peak_rss_kb, int(line.split()[1]))
                            break
                except FileNotFoundError:
                    return
                await asyncio.sleep(0.5)

    async def boot(self) -> None:
        """Start the VM/container under a timeout wrapper."""

        cmd = self._boot_command()
        print_cmd_line(cmd)

        # Redirect both streams into a per-machine boot log so the chatty
        # systemd init / qemu console doesn't drown out the test transcript.
        # The kernel writes straight to disk, so no pipe buffer to drain.
        # stderr=STDOUT merges FD 2 onto FD 1 in the kernel before any write
        # happens, so the on-disk order is exactly the syscall order across
        # both streams -- the price is that we can no longer tell which line
        # came from stderr (no per-stream coloring).
        # start_new_session=True puts the child in its own process group so
        # terminal SIGINT only hits the python parent; we drive child
        # shutdown explicitly through Machine.stop().
        with self.boot_file.open("wb") as handle:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=handle,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        # Parent's handle can close once the child holds its own dup'd FD.
        # stdin=DEVNULL keeps qemu's `-serial stdio` (and any podman quirk)
        # from competing with the parent terminal for keystrokes, especially
        # under parallel testall.py runs.

    async def ensure_booted(self) -> None:
        """Block until the hypervisor writes the PID/CID file or the launch fails."""

        deadline = time.monotonic() + IDFILE_TIMEOUT
        id_path = Path(f"{self.workdir.name}/{self.idfile}")
        while not id_path.exists():
            if self.proc and self.proc.returncode is not None:
                raise RuntimeError("Launching machine failed")
            if time.monotonic() > deadline:
                raise TimeoutError(f"PID file {id_path} not created within {IDFILE_TIMEOUT}s")
            await sleep_tick()

    async def ensure_ssh(self) -> None:
        """Resolve SSH port then wait for the daemon banner to appear."""

        await self._find_ssh_port()

        deadline = time.monotonic() + SSH_WAIT_TIMEOUT
        while not await self._ssh_banner_ready():
            if time.monotonic() > deadline:
                raise TimeoutError("SSH daemon did not become ready in time")
            await sleep_tick()

    async def _ssh_banner_ready(self) -> bool:
        """Probe the SSH port once. Return True iff a non-empty banner arrives."""

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.ssh_host, self.ssh_port),
                timeout=2,
            )
        except (OSError, TimeoutError):
            # sshd not yet accepting connections; caller will retry.
            return False

        try:
            banner_bytes = await asyncio.wait_for(reader.read(1024), timeout=2)
            return bool(banner_bytes.decode().strip())
        except (OSError, TimeoutError):
            # Connected but no banner in time; treat as not-ready.
            return False
        finally:
            writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                # Half-open peer or already-broken transport: don't let a
                # stuck close stall the polling loop.
                await asyncio.wait_for(writer.wait_closed(), timeout=1)

    async def collect_journal(self) -> None:
        """Fetch systemd journal for debugging when a run fails."""

        cmd = self.format_ssh_cmd(
            "env",
            "SYSTEMD_COLORS=true",
            "journalctl",
            "--no-pager",
            "--priority",
            "info",
        )
        print_cmd_line(cmd)

        with self.journal_file.open("w") as handle:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=handle, stderr=asyncio.subprocess.PIPE,
            )
            # stderr only -- stdout is already going to the journal file. Read
            # to EOF before waiting so a chatty stderr can't deadlock the child
            # by filling the pipe buffer.
            # Ordering: stdout lines land in journal_file in source order
            # (single FD, kernel FIFO); stderr lines land in the main log in
            # source order. The two streams go to different destinations so
            # there's no cross-stream interleave to worry about here.
            await read_and_write_stream(proc.stderr, "stderr", [])
            exitcode = await proc.wait()

        if exitcode != 0:
            print_line(f"Failed to collect journal: exit code {exitcode}")
            return
        print_line(f"Systemd journal: {self.journal_file}")

    def print_journal_tail(self, n: int = 50) -> None:
        """Print the last *n* lines of the saved journal to stdout."""
        self._print_file_tail(self.journal_file, n)

    def print_boot_tail(self, n: int = 50) -> None:
        """Print the last *n* lines of the boot subprocess log to stdout."""
        self._print_file_tail(self.boot_file, n)

    def cleanup_logs(self) -> None:
        """Remove all per-run log artifacts (output, boot, journal)."""
        for path in (self.output_file, self.boot_file, self.journal_file):
            path.unlink(missing_ok=True)

    def _print_file_tail(self, path: Path, n: int) -> None:
        if not path.exists():
            return
        # Stream through a bounded deque so a multi-MB boot log (panic loop,
        # chatty cloud-init) doesn't get fully loaded into memory just to
        # slice off the last N lines.
        with path.open("r", errors="replace") as handle:
            tail = list(collections.deque(handle, maxlen=n))
        tail = [line.rstrip("\n") for line in tail]
        print_line(f"--- last {len(tail)} lines of {path} ---")
        for line in tail:
            print_line(line)
        print_line(f"--- end {path} ---")

    def print_ssh_instructions(self) -> None:
        ssh_cmd = shlex.join(self.format_ssh_cmd())
        print_line("Keeping VM around, ssh using:")
        print_line(f"> {ssh_cmd}")
        print_line("Then Ctrl+C to stop the machine")

    async def wait(self) -> None:
        if self.proc:
            await self.proc.wait()

    async def __aenter__(self) -> Self:
        await self.prepare()
        await self.boot()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        print_line("Stopping machine...")
        await self.stop()
        # Surface the tail of the boot/console log on infra-shaped failures so
        # the main transcript ends with the most likely diagnostic. Cancellation
        # is the user wanting out; idempotence checks fail at the role layer
        # and the boot log won't help.
        if (
            isinstance(exc_type, type)
            and issubclass(exc_type, BaseException)
            and not issubclass(exc_type, (asyncio.CancelledError, IdempotenceFailedException))
        ):
            self.print_boot_tail()

    async def stop(self) -> None:
        """Drain the boot subprocess and free temp resources.

        Subclasses perform hypervisor-specific cleanup (qemu kill, podman rm)
        before delegating here, so this final drain only sees a process
        that's already on its way out.
        """
        try:
            if self.proc and self.proc.returncode is None:
                await terminate_subprocess(
                    self.proc, grace_seconds=5, initial_signal=signal.SIGINT,
                )
        finally:
            self.workdir.cleanup()


class QemuMachine(Machine):
    """Start disposable QEMU guests for role-level integration tests."""
    drives: list[str]
    _sampler_task: asyncio.Task[None] | None

    def __init__(self, machine: str, role: str, keep_vm: bool, ubuntu_name: str):
        """QEMU-backed machine wrapper used by integration tests."""
        try:
            ssh_user, ansible_args, inventory_host = QEMU_MACHINE_ARGS[machine]
        except KeyError:
            raise AttributeError(f"Unknown machine: {machine}") from None

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
            ubuntu_name=ubuntu_name,
        )
        self._sampler_task = None

    async def prepare(self) -> None:
        """Create overlay images and seed data required for the selected QEMU template."""

        await super().prepare()

        if self.machine == "minimal":
            await run_command(
                ["cloud-localds", f"{self.workdir.name}/seed.img", "test/minimal/user-data", "test/minimal/meta-data"],
            )
            await self._create_overlay(
                f"{self.imagedir}/ubuntu-{self.ubuntu_version}-minimal-cloudimg-amd64.img",
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

    async def _create_overlay(self, src: str, dest: str, size: str | None = None) -> None:
        """Create a qcow2 overlay pointing at *src* with optional resize."""

        args = ["qemu-img", "create", "-f", "qcow2", "-b", src, "-F", "qcow2", dest]
        if size:
            args.append(size)
        await run_command(args)

    async def _copy_efivars(self) -> None:
        """Copy EFI vars for UEFI boots into the working directory."""

        await run_command(
            ["cp", f"{self.imagedir}/{self.ubuntu_name}/ubuntu-{self.machine}/efivars.fd", f"{self.workdir.name}/efivars.fd"],
        )

    async def _create_overlay_series(self, count: int) -> None:
        """Create a numbered set of overlays (packer-ubuntu-1..count)."""

        for idx in range(1, count + 1):
            await self._create_overlay(
                f"{self.imagedir}/{self.ubuntu_name}/ubuntu-{self.machine}/packer-ubuntu-{idx}",
                f"{self.workdir.name}/packer-ubuntu-{idx}",
            )

    def _virtio_drive(self, path: str) -> str:
        """Return a virtio drive string with sensible cache/discard flags."""

        return f"file={path},if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap"

    def _virtio_drive_series(self, count: int) -> Iterable[str]:
        return (self._virtio_drive(f"{self.workdir.name}/packer-ubuntu-{idx}") for idx in range(1, count + 1))

    def _uefi_drives(self) -> list[str]:
        return [
            "file=/usr/share/OVMF/OVMF_CODE.fd,if=pflash,unit=0,format=raw,readonly=on",
            f"file={self.workdir.name}/efivars.fd,if=pflash,unit=1,format=raw",
        ]

    def _boot_command(self) -> list[str]:
        """Assemble qemu-system-x86_64 command line for the prepared disks."""

        if self.keep_vm:
            display_args = ["-display", "vnc=:0,to=99", "-vga", "std", "-usb", "-device", "usb-tablet", "-k", "fr"]
        else:
            display_args = ["-display", "none"]

        return [
            "timeout",
            "--kill-after=10s",
            MACHINE_TIMEOUT,
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
            # Wire the guest's first serial port (ttyS0, where Ubuntu's kernel
            # cmdline points "console=") to qemu's stdio. With Machine.boot()
            # redirecting stdout to boot_file, this lands the kernel ring
            # buffer + early systemd output in the per-machine boot log.
            "-serial",
            "stdio",
            "-device",
            "virtio-net,netdev=user.0",
            "-pidfile",
            f"{self.workdir.name}/{self.idfile}",
        ]

    async def ensure_booted(self) -> None:
        await super().ensure_booted()
        pid = int(Path(f"{self.workdir.name}/{self.idfile}").read_text().strip())
        self._sampler_task = asyncio.create_task(self._sample_peak_rss(pid))

    async def stop(self) -> None:
        """Kill qemu via its pidfile, then drain the timeout wrapper.

        Signaling self.proc (the `timeout` wrapper) normally forwards SIGINT
        to qemu, but if the wrapper is SIGKILL'd or testrole.py dies before
        stop() runs, qemu reparents to init with no recovery path -- SIGKILL
        can't be caught and forwarded. Kill qemu directly via its pidfile so
        cleanup works regardless of the wrapper's fate.
        """
        pid_path = Path(f"{self.workdir.name}/{self.idfile}")
        pid: int | None = None
        if pid_path.exists():
            with contextlib.suppress(ValueError):
                pid = int(pid_path.read_text().strip())

        try:
            try:
                if pid is not None:
                    # Shield against nested cancellation; without it a second
                    # SIGINT mid-cleanup would leave qemu running.
                    await asyncio.shield(self._terminate_qemu(pid))
            finally:
                await super().stop()
        finally:
            if self._sampler_task is not None:
                self._sampler_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._sampler_task

    async def _terminate_qemu(self, pid: int) -> None:
        """SIGTERM qemu, poll briefly, escalate to SIGKILL if it lingers."""
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.2)

        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)

    async def _find_ssh_port(self) -> None:
        """Parse lsof output from the QEMU pidfile to extract forwarded SSH port."""
        pid = Path(f"{self.workdir.name}/{self.idfile}").read_text().strip()
        if not pid:
            raise RuntimeError("Missing qemu PID; pidfile is empty")

        # -sTCP:LISTEN drops ESTABLISHED rows so we never pick up a guest's
        # outbound connection by accident. -n avoids DNS reverse-lookup
        # latency on every poll.
        lsof_cmd = ["lsof", "-iTCP", "-sTCP:LISTEN", "-P", "-n", "-p", pid]

        lines: list[str] = []
        for _ in range(10):
            lines = (await run_command(lsof_cmd)).stdout

            for line in lines:
                fields = line.split()
                # Data rows: COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME [STATE]
                if len(fields) < 9 or fields[1] != pid or fields[7] != "TCP":
                    continue

                # NAME column carries "host:port" for LISTEN sockets;
                # rsplit handles IPv4 (127.0.0.1:port) and IPv6 ([::]:port).
                addr = fields[8]
                if ":" not in addr:
                    continue
                port = int(addr.rsplit(":", 1)[-1])

                # VNC displays :0..:99 listen on 5900..5999 — skip those when
                # --keep adds -display vnc.
                if 5900 <= port <= 5999:
                    continue

                self.ssh_port = port
                return

            await sleep_tick()

        lsof_dump = "\n".join(lines) if lines else "<no output>"
        raise RuntimeError(
            f"Unable to determine SSH port from qemu lsof output (pid {pid}):\n{lsof_dump}"
        )


class PodmanMachine(Machine):
    """Start privileged Podman containers that mimic SSH hosts."""

    def __init__(self, machine: str, role: str, keep_vm: bool, ubuntu_name: str):
        """Podman-backed machine wrapper used by integration tests."""
        system = platform.system()
        if system == "Darwin":
            imagedir = os.environ.get("TMPDIR", "/tmp")
        elif system == "Linux":
            imagedir = "/mnt/qemu"
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
            ubuntu_name=ubuntu_name,
        )

    async def prepare(self) -> None:
        """Create podman network if missing and stage working dir."""

        await super().prepare()

        await ensure_podman_network()

    def _boot_command(self) -> list[str]:
        """Return the podman run command that exposes SSH on a random host port."""

        return [
            "podman",
            "run",
            "--rm",
            "--timeout",
            MACHINE_TIMEOUT,
            "--systemd",
            "always",
            "--hostname",
            self.inventory_host,
            "--publish",
            "127.0.0.1::22",
            "--privileged",
            "--cidfile",
            f"{self.workdir.name}/{self.idfile}",
            "--network",
            PODMAN_NETWORK,
            f"homelab:{self.ubuntu_name}",
        ]

    async def stop(self) -> None:
        """Tear down the container via cidfile, then drain the foreground client.

        Rootless podman containers are supervised by `conmon`, which detaches
        from the foreground `podman run` client. Signaling the client alone
        (the base-class default) doesn't reliably stop the container -- if
        the client is SIGKILL'd or testrole.py is interrupted before cleanup
        finishes, conmon and the container survive reparented to init.
        Talking to conmon directly through `podman rm --force <cid>` works
        regardless of the client's state.
        """
        cid_path = Path(f"{self.workdir.name}/{self.idfile}")
        cid = cid_path.read_text().strip() if cid_path.exists() else None

        try:
            if cid:
                # asyncio.shield prevents a second SIGINT from cancelling the
                # rm mid-flight; without it, nested cancellation leaks the
                # container.
                await asyncio.shield(
                    run_command(
                        ["podman", "rm", "--force", "--time", "5", cid],
                        check=False,
                    )
                )
        finally:
            await super().stop()

    async def _find_ssh_port(self) -> None:
        """Ask podman for the forwarded SSH port and store it on the instance."""

        cid = Path(f"{self.workdir.name}/{self.idfile}").read_text().strip()
        if not cid:
            raise RuntimeError("Missing container ID; podman run may have failed")

        result = await run_command(["podman", "port", cid, "22"])
        addr = "\n".join(result.stdout).strip()
        if ":" not in addr:
            raise RuntimeError(f"Unexpected podman port output: {addr}")

        self.ssh_port = int(addr.rsplit(":", 1)[-1])
