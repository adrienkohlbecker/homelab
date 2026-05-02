#!/usr/bin/env -S uv run

import asyncio
import collections
import contextlib
import dataclasses
import fcntl
import os
import platform
import shlex
import shutil
import signal
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple, Self

from setup_mitogen import ensure_mitogen_symlink
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

# Repair the .ansible-mitogen-strategy symlink at module-import time so every
# testrole.py / testall.py invocation refreshes it after a `uv sync` Python
# bump. Direct ansible runs by the user share the same symlink; if it's
# dangling, ansible-playbook fails loudly with "Invalid play strategy".
ensure_mitogen_symlink()

OUT_DIR = Path("test/out")
UBUNTU_RELEASES: dict[str, str] = {
    "jammy": "22.04",
    "noble": "24.04",
}
DEFAULT_UBUNTU = "jammy"
SSH_KEY = "packer/vagrant.key"
SSH_HOST = "127.0.0.1"

CONTAINER_ANSIBLE_ARGS = ["-e", '{"docker_test":true}', "-e", "@host_vars/box-podman.yml"]


class QemuMachineSpec(NamedTuple):
    ssh_user: str
    ansible_args: list[str]
    inventory_host: str
    disk_count: int  # number of packer-ubuntu-N overlays to stage; 0 for cloud-init seed disk


QEMU_MACHINE_SPECS: dict[str, QemuMachineSpec] = {
    "minimal": QemuMachineSpec(
        ssh_user="ubuntu",
        ansible_args=["-e", '{"qemu_test":true,"qemu_test_minimal":true}', "-e", "@host_vars/box-qemu-minimal.yml"],
        inventory_host="box",
        disk_count=0,
    ),
    "box": QemuMachineSpec(
        ssh_user="vagrant",
        ansible_args=["-e", '{"qemu_test":true,"qemu_test_minimal":false}', "-e", "@host_vars/box-qemu.yml"],
        inventory_host="box",
        disk_count=1,
    ),
    "lab": QemuMachineSpec(
        ssh_user="vagrant",
        ansible_args=["-e", '{"qemu_test":true,"qemu_test_minimal":false}', "-e", "@host_vars/lab-qemu.yml"],
        inventory_host="lab",
        disk_count=9,
    ),
    "pug": QemuMachineSpec(
        ssh_user="vagrant",
        ansible_args=["-e", '{"qemu_test":true,"qemu_test_minimal":false}', "-e", "@host_vars/pug-qemu.yml"],
        inventory_host="pug",
        disk_count=3,
    ),
}
MACHINE_CHOICES: tuple[str, ...] = ("container", *QEMU_MACHINE_SPECS)

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


def ubuntu_mirrors(upstream: bool = False) -> tuple[str, str]:
    """Return archive and security mirrors for the current CPU architecture.

    With upstream=True, return Ubuntu's public mirrors instead of the local
    Nexus cache — useful when the lab mirror is unreachable.
    """
    arch = platform.machine().lower()
    if arch in {"aarch64", "arm64"}:
        if upstream:
            return (
                "http://ports.ubuntu.com/ubuntu-ports/",
                "http://ports.ubuntu.com/ubuntu-ports/",
            )
        return (
            "http://nexus.lab.fahm.fr/repository/ubuntu-ports/",
            "http://nexus.lab.fahm.fr/repository/ubuntu-ports/",
        )
    if arch == "x86_64":
        if upstream:
            return (
                "http://archive.ubuntu.com/ubuntu/",
                "http://security.ubuntu.com/ubuntu/",
            )
        return (
            "http://nexus.lab.fahm.fr/repository/ubuntu-archive/",
            "http://nexus.lab.fahm.fr/repository/ubuntu-security/",
        )
    raise SystemExit("Unknown machine name")


def podman_registry_mirrors(upstream: bool = False) -> dict[str, str]:
    """Return upstream registry → pull-through mirror endpoint for podman.

    With upstream=True, return an empty mapping so callers skip writing a
    registries.conf drop-in and let podman pull straight from the upstream.
    """
    if upstream:
        return {}
    return {
        "docker.io": "nexus.lab.fahm.fr/docker.io",
        "ghcr.io": "nexus.lab.fahm.fr/ghcr.io",
    }


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
    machine_timeout: int
    upstream_mirrors: bool = False

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

    @property
    def wrapper_timeout(self) -> int:
        """Last-resort timeout passed to coreutils `timeout` / podman --timeout.

        0 disables the wrapper (`timeout 0` runs forever, podman --timeout 0
        is "no timeout") so an interactive --keep session isn't cut short.
        Otherwise it tracks the Python --timeout plus a small grace window.
        """
        return 0 if self.keep_vm else self.machine_timeout

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
        ubuntu_mirror, ubuntu_mirror_security = ubuntu_mirrors(upstream=self.upstream_mirrors)
        # Fact cache lives inside the per-run workdir, so the ~9 ansible-playbook
        # invocations in one test share gathered facts (saves ~0.9s per replay)
        # without leaking facts across runs that target a freshly-spawned host
        # (different IP, different cgroup, different filesystem).
        fact_cache = f"{self.workdir.name}/facts"
        parts = [
            "env",
            "ANSIBLE_DISPLAY_OK_HOSTS=true",
            "ANSIBLE_DISPLAY_SKIPPED_HOSTS=true",
            "ANSIBLE_GATHERING=smart",
            "ANSIBLE_FACT_CACHING=jsonfile",
            f"ANSIBLE_FACT_CACHING_CONNECTION={fact_cache}",
            "ANSIBLE_FACT_CACHING_TIMEOUT=7200",
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
            assert proc.stderr is not None
            # stderr only -- stdout is already going to the journal file. Read
            # to EOF before waiting so a chatty stderr can't deadlock the child
            # by filling the pipe buffer.
            # Ordering: stdout lines land in journal_file in source order
            # (single FD, kernel FIFO); stderr lines land in the main log in
            # source order. The two streams go to different destinations so
            # there's no cross-stream interleave to worry about here.
            await read_and_write_stream(proc.stderr, "red", [])
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


def _read_vm_hwm(pid: int) -> int:
    """Return the kernel-tracked peak RSS in kB for *pid*, or 0 if unreadable.

    VmHWM ("high-water mark") in /proc/<pid>/status is monotonic and maintained
    by the kernel, so a single read at process exit gives the exact peak —
    no sampling loop required.
    """
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError, ValueError):
        pass
    return 0


class QemuMachine(Machine):
    """Start disposable QEMU guests for role-level integration tests."""
    drives: list[str]

    def __init__(self, machine: str, role: str, keep_vm: bool, ubuntu_name: str, machine_timeout: int, upstream_mirrors: bool = False):
        """QEMU-backed machine wrapper used by integration tests."""
        try:
            spec = QEMU_MACHINE_SPECS[machine]
        except KeyError:
            raise AttributeError(f"Unknown machine: {machine}") from None

        self._spec = spec
        super().__init__(
            ssh_port=0,
            ssh_user=spec.ssh_user,
            ansible_args=spec.ansible_args,
            inventory_host=spec.inventory_host,
            idfile="pid",
            imagedir="/mnt/qemu",
            machine=machine,
            role=role,
            keep_vm=keep_vm,
            ubuntu_name=ubuntu_name,
            machine_timeout=machine_timeout,
            upstream_mirrors=upstream_mirrors,
        )

    async def prepare(self) -> None:
        """Create overlay images and seed data required for the selected QEMU template."""

        await super().prepare()

        if self.machine == "minimal":
            if shutil.which("cloud-localds") is None:
                raise RuntimeError(
                    "cloud-localds not found in PATH — install cloud-image-utils "
                    "(`apt install cloud-image-utils`) to use the minimal machine."
                )
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

        for idx in range(1, self._spec.disk_count + 1):
            await self._create_overlay(
                f"{self.imagedir}/{self.ubuntu_name}/ubuntu-{self.machine}/packer-ubuntu-{idx}",
                f"{self.workdir.name}/packer-ubuntu-{idx}",
            )
        await self._copy_efivars()
        self.drives = [
            *(self._virtio_drive(f"{self.workdir.name}/packer-ubuntu-{idx}")
              for idx in range(1, self._spec.disk_count + 1)),
            *self._uefi_drives(),
        ]

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

    def _virtio_drive(self, path: str) -> str:
        """Return a virtio drive string with sensible cache/discard flags."""

        return f"file={path},if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap"

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
            str(self.wrapper_timeout),
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

        if pid is not None:
            # Snapshot kernel-tracked peak RSS before we kill qemu. VmHWM is
            # monotonic so a single read is exact; doing it here also covers
            # the --keep case where the user's interactive session can have
            # added to the high-water mark after the test body finished.
            self.peak_rss_kb = _read_vm_hwm(pid)

        try:
            if pid is not None:
                # Shield against nested cancellation; without it a second
                # SIGINT mid-cleanup would leave qemu running.
                await asyncio.shield(self._terminate_qemu(pid))
        finally:
            await super().stop()

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


def is_service_role(role: str) -> bool:
    """True if the role's _test.yml imports `_test/podman` or `_test/nginx`.

    Drives PodmanMachine's image pick: service roles use the pre-baked
    `homelab-service:<release>` so their _test imports skip via the
    existing `creates:` sentinels.
    """
    test_yml = Path(f"roles/{role}/tasks/_test.yml")
    if not test_yml.exists():
        return False
    text = test_yml.read_text()
    return "tasks_from: podman" in text or "tasks_from: nginx" in text


BAKE_HASH_LABEL = "homelab.bake-hash"
_BAKE_HASH_INPUTS = (
    Path("test/Dockerfile"),
    Path("roles/_bake/tasks/main.yml"),
    Path("roles/_test/tasks/podman.yml"),
    Path("roles/_test/tasks/nginx.yml"),
)


def _bake_inputs_hash() -> str:
    """sha256 over the files that drive the homelab-service bake.

    Used to label the committed image and to short-circuit a rebake when
    nothing relevant has changed.
    """
    import hashlib
    h = hashlib.sha256()
    for p in _BAKE_HASH_INPUTS:
        h.update(p.read_bytes())
    return h.hexdigest()


async def existing_image_hash(tag: str) -> str | None:
    """Return the bake-hash label on *tag*, or None if missing/unlabeled."""
    res = await run_command(
        ["podman", "image", "inspect", "--format",
         "{{ index .Config.Labels \"" + BAKE_HASH_LABEL + "\" }}", tag],
        check=False, quiet=True,
    )
    if res.exitcode != 0:
        return None
    return "\n".join(res.stdout).strip() or None


class PodmanMachine(Machine):
    """Start privileged Podman containers that mimic SSH hosts."""

    commit_image: str | None

    def __init__(self, machine: str, role: str, keep_vm: bool, ubuntu_name: str, machine_timeout: int, upstream_mirrors: bool = False, commit_image: str | None = None):
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
            machine_timeout=machine_timeout,
            upstream_mirrors=upstream_mirrors,
        )
        self.commit_image = commit_image

    @property
    def image_tag(self) -> str:
        """`homelab-service:<release>` for podman-service roles, else `homelab:<release>`."""
        repo = "homelab-service" if is_service_role(self.role) else "homelab"
        return f"{repo}:{self.ubuntu_name}"

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
            str(self.wrapper_timeout),
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
            self.image_tag,
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

        if cid:
            # Snapshot cgroup memory.peak before rm tears the cgroup down.
            self.peak_rss_kb = await self._read_container_peak_kb(cid)

        try:
            if cid and self.commit_image:
                # Commit the live container as the configured image before
                # rm tears it down. shield: a second SIGINT can't half-bake
                # the image. The bake-hash label lets future runs cache-hit.
                bake_hash = _bake_inputs_hash()
                await asyncio.shield(
                    run_command(
                        ["podman", "commit",
                         "--change", f"LABEL homelab.bake-hash={bake_hash}",
                         cid, self.commit_image],
                        check=False,
                    )
                )
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

    async def _read_container_peak_kb(self, cid: str) -> int:
        """Return the cgroup-tracked peak memory in kB for the container.

        Reads memory.peak (cgroup v2; kernel-tracked high-water mark for the
        whole container, not just PID 1). Returns 0 on cgroup v1 hosts or any
        other failure — the caller already treats 0 as "no measurement".
        """
        inspect = await run_command(
            ["podman", "inspect", "--format", "{{.State.CgroupPath}}", cid],
            check=False,
        )
        if inspect.exitcode != 0:
            return 0
        cgroup_path = "\n".join(inspect.stdout).strip()
        if not cgroup_path:
            return 0

        peak_file = Path("/sys/fs/cgroup") / cgroup_path.lstrip("/") / "memory.peak"
        try:
            return int(peak_file.read_text().strip()) // 1024
        except (FileNotFoundError, PermissionError, ValueError):
            return 0

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
