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
    # Packer image directory under /mnt/qemu/<ubuntu_name>/. None means the
    # variant uses an Ubuntu cloud image instead (minimal).
    packer_image: str | None
    # Sizes of additional empty qcow2 disks attached at boot beyond the OS
    # disk, e.g. ["1G", "1G"] for pug. The OS disk is always vda; these get
    # vdb, vdc, ... in attachment order. The disk_setup_script consumes them
    # by position.
    extra_disks: list[str]
    # Path to a shell script under test/disks/ run via SSH+sudo after the VM
    # is reachable, before any role/_setup playbook. Receives the extra disk
    # devices as positional args. None means no setup needed.
    disk_setup_script: str | None


QEMU_MACHINE_SPECS: dict[str, QemuMachineSpec] = {
    "minimal": QemuMachineSpec(
        ssh_user="ubuntu",
        ansible_args=["-e", '{"qemu_test":true,"qemu_test_minimal":true}', "-e", "@host_vars/box-qemu-minimal.yml"],
        inventory_host="box",
        packer_image=None,
        extra_disks=[],
        disk_setup_script=None,
    ),
    "box": QemuMachineSpec(
        ssh_user="vagrant",
        ansible_args=["-e", '{"qemu_test":true,"qemu_test_minimal":false}', "-e", "@host_vars/box-qemu.yml"],
        inventory_host="box",
        packer_image="ubuntu-zfs",
        extra_disks=[],
        disk_setup_script=None,
    ),
    "lab": QemuMachineSpec(
        ssh_user="vagrant",
        ansible_args=["-e", '{"qemu_test":true,"qemu_test_minimal":false}', "-e", "@host_vars/lab-qemu.yml"],
        inventory_host="lab",
        # ubuntu-zfs-lab brings the 3-disk mirror rpool baked in (matches
        # the lab-class prod host). Extras below add the dozer/tank/mouse
        # disks layered on top by test/disks/lab.sh.
        packer_image="ubuntu-zfs-lab",
        # Six disks: dozer mirror legs (1G ×2), tank/mouse shared hosts
        # (1.5G ×2 — partitioned at test time), plus two whole-disk tank
        # raidz2 vdevs (1G ×2). Sizes match the previous packer-baked layout.
        extra_disks=["1G", "1G", "1.5G", "1.5G", "1G", "1G"],
        disk_setup_script="test/disks/lab.sh",
    ),
    "pug": QemuMachineSpec(
        ssh_user="vagrant",
        ansible_args=["-e", '{"qemu_test":true,"qemu_test_minimal":false}', "-e", "@host_vars/pug-qemu.yml"],
        inventory_host="pug",
        packer_image="ubuntu-zfs",
        extra_disks=["1G", "1G"],
        disk_setup_script="test/disks/pug.sh",
    ),
}
MACHINE_CHOICES: tuple[str, ...] = ("container", *QEMU_MACHINE_SPECS)

# Number of qcow2 disks each packer image stages as part of the OS install.
# ubuntu-zfs is single-rpool, ubuntu-zfs-lab is a 3-disk mirror rpool. The
# prepare() code overlays packer-ubuntu-1..N for the OS, then attaches the
# variant's extra_disks starting at vd[a+N].
_PACKER_IMAGE_OS_DISKS: dict[str, int] = {
    "ubuntu-zfs": 1,
    "ubuntu-zfs-lab": 3,
}

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
            raise ValueError(f"Unknown Ubuntu release '{self.ubuntu_name}'; known: {sorted(UBUNTU_RELEASES)}")
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
            # The wait-for-ready loop runs before ansible-playbook does, so
            # whichever of these creates the ControlMaster (per the user's
            # ~/.ssh/config Host *: ControlMaster auto) decides whether the
            # master has an agent-forwarding channel. Without `-A` here, the
            # master comes up without one, and ansible's later ForwardAgent=yes
            # silently reuses the agent-less master -- breaking roles that
            # ssh out to git@github.com from the test target.
            "-o",
            "ForwardAgent=yes",
            f"{self.ssh_user}@{self.ssh_host}",
        ]
        return [*base, shlex.join(cmd)] if cmd else base

    def format_scp_cmd(self, local: str, remote: str) -> list[str]:
        """Return an scp invocation pinned to this instance.

        Uses the same `-o` flags as `format_ssh_cmd`; only the port flag
        differs (scp uses `-P`, ssh uses `-p`).
        """
        return [
            "scp",
            "-i",
            self.ssh_key,
            "-P",
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
            local,
            f"{self.ssh_user}@{self.ssh_host}:{remote}",
        ]

    def format_ansible_cmd(self, *cmd: str) -> list[str]:
        """Build an ansible-playbook command pinned to this machine's SSH details."""
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
            "--inventory",
            "test/inventory.ini",
            *self.ansible_args,
        ]
        # --upstream-mirrors clears nexus_url so all mirror_* Jinja in
        # group_vars/all.yml resolves to upstream URLs even though
        # group_vars/test.yml sets nexus_url.
        if self.upstream_mirrors:
            parts += ["-e", "nexus_url="]
        if cmd:
            parts += cmd
        return parts

    async def ssh_command(self, *cmd: str, check: bool = True) -> CommandResult:
        """Execute an SSH command and stream output into the role log."""

        return await run_command(self.format_ssh_cmd(*cmd), check=check)

    async def ansible_command(self, *cmd: str, check: bool = True) -> CommandResult:
        """Execute ansible-playbook with machine-specific SSH overrides."""

        return await run_command(self.format_ansible_cmd(*cmd), check=check)

    async def run_disk_setup(self) -> None:
        """Run the variant's post-boot disk-setup script over SSH, if any.

        Default no-op for machines that don't carry extra disks (container,
        minimal, box). QemuMachine overrides for variants whose spec sets a
        disk_setup_script (lab, pug).
        """
        return

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

        # Mirror setup runs once at the start of every test (before role hooks)
        # to point apt / podman / pip / uv at the lab Nexus when nexus_url is
        # set. Replaces the SSH-driven apt + podman setup that lived in
        # testrole.py.
        Path(f"{self.workdir.name}/_mirrors.yml").write_text(
            f"""
- hosts: {self.inventory_host}
  tasks:
    - import_role:
        name: _test
        tasks_from: mirrors
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
                *cmd,
                stdout=handle,
                stderr=asyncio.subprocess.PIPE,
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
        if isinstance(exc_type, type) and issubclass(exc_type, BaseException) and not issubclass(exc_type, (asyncio.CancelledError, IdempotenceFailedException)):
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
                    self.proc,
                    grace_seconds=5,
                    initial_signal=signal.SIGINT,
                )
        finally:
            self.workdir.cleanup()


# cloud-init script consumed by _extract_kernel_initrd's one-shot VM. apt-installs
# zfsutils-linux, mounts the host 9p share, imports the rpool from the attached
# packer qcow2(s), copies the highest-versioned on-pool kernel + initrd, and
# composes a ZBM-style cmdline ("root=zfs=<bootfs> <org.zfsbootmenu:commandline>")
# read off rpool/ROOT before powering off. The poweroff exit is how the host
# knows extraction finished -- the qemu process exits cleanly.
EXTRACTION_USER_DATA = """\
#cloud-config
package_update: true
packages:
  - zfsutils-linux
runcmd:
  - |
    set -eux
    mkdir -p /share
    modprobe 9pnet_virtio || true
    mount -t 9p -o trans=virtio,version=9p2000.L share /share
    zpool import -fN -R /mnt rpool
    active=$(zpool get -H -o value bootfs rpool)
    if [ -z "$active" ] || [ "$active" = "-" ]; then
        active=$(zfs list -H -o name -t filesystem | grep -m1 '^rpool/ROOT/')
    fi
    zfs mount "$active"
    mp=$(findmnt -nro TARGET --source "$active")
    kernel=$(ls "$mp/boot/"vmlinuz-* "$mp/boot/"vmlinux-* 2>/dev/null | sort -V | tail -1)
    initrd=$(ls "$mp/boot/"initrd.img-* 2>/dev/null | sort -V | tail -1)
    cp -L "$kernel" /share/kernel
    cp -L "$initrd" /share/initrd
    zbm_args=$(zfs get -H -o value org.zfsbootmenu:commandline rpool/ROOT)
    [ "$zbm_args" = "-" ] && zbm_args=""
    printf 'root=zfs=%s %s' "$active" "$zbm_args" > /share/cmdline
    sync
    touch /share/done
power_state:
  mode: poweroff
  delay: now
"""


def _qcow2_fingerprint(paths: list[Path]) -> str:
    """sha256 over the OS qcow2(s) -- cache key for extracted kernel/initrd.

    Reads all bytes; on a 1 GiB packer image this takes a few seconds. Sorted
    by path so a multi-disk variant (ubuntu-zfs-lab's 3-way mirror) yields a
    stable digest regardless of iteration order. Any packer rebuild changes
    the qcow2 contents, which invalidates the cache and re-runs extraction.
    """
    import hashlib

    h = hashlib.sha256()
    for p in sorted(paths):
        with p.open("rb") as f:
            while chunk := f.read(1024 * 1024):
                h.update(chunk)
    return h.hexdigest()


async def _ensure_extraction_cloudimg(imagedir: str, ubuntu_name: str, arch: str) -> Path:
    """Download (once) the upstream Ubuntu cloud image used as the extraction vehicle."""
    cloud_arch = "amd64" if arch == "x86_64" else "arm64"
    name = f"{ubuntu_name}-server-cloudimg-{cloud_arch}.img"
    cache = Path(imagedir) / "cloud-images"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / name
    if target.exists():
        return target

    url = f"https://cloud-images.ubuntu.com/{ubuntu_name}/current/{name}"
    tmp = target.with_suffix(target.suffix + ".tmp")
    print_line(f"Downloading {url}")
    await run_command(["curl", "-fL", "--retry", "3", "-o", str(tmp), url])
    tmp.rename(target)
    return target


def _uefi_code_path(arch: str) -> Path:
    """Locate the EDK2/OVMF CODE blob for *arch* on the host.

    Searches Homebrew (macOS) plus the canonical package paths on Debian/
    Ubuntu and Fedora/RHEL. Raises RuntimeError if none of the candidates
    exist.
    """
    by_arch = {
        "aarch64": [
            # Homebrew QEMU on macOS:
            "/opt/homebrew/share/qemu/edk2-aarch64-code.fd",
            "/usr/local/share/qemu/edk2-aarch64-code.fd",
            # Debian/Ubuntu (qemu-efi-aarch64 package):
            "/usr/share/AAVMF/AAVMF_CODE.fd",
            "/usr/share/qemu-efi-aarch64/QEMU_EFI.fd",
        ],
        "x86_64": [
            # Homebrew QEMU on macOS:
            "/opt/homebrew/share/qemu/edk2-x86_64-code.fd",
            "/usr/local/share/qemu/edk2-x86_64-code.fd",
            # Debian/Ubuntu (ovmf package):
            "/usr/share/OVMF/OVMF_CODE.fd",
            # Fedora/RHEL (edk2-ovmf package):
            "/usr/share/edk2/ovmf/OVMF_CODE.fd",
            "/usr/share/edk2-ovmf/x64/OVMF_CODE.fd",
        ],
    }
    candidates = by_arch.get(arch)
    if not candidates:
        raise RuntimeError(f"Unsupported arch for UEFI lookup: {arch}")
    for c in candidates:
        if Path(c).exists():
            return Path(c)
    raise RuntimeError(
        f"No {arch} UEFI firmware found in {candidates}. "
        "Install via `brew install qemu` (macOS), "
        "`apt install ovmf` / `apt install qemu-efi-aarch64` (Debian/Ubuntu), or "
        "`dnf install edk2-ovmf` (Fedora/RHEL)."
    )


async def _build_seed_iso(out: Path, user_data: Path, meta_data: Path) -> None:
    """Pack a NoCloud cidata seed iso for the extraction VM's cloud-init."""
    if shutil.which("cloud-localds"):
        await run_command(["cloud-localds", str(out), str(user_data), str(meta_data)])
        return
    iso_tool = shutil.which("xorrisofs") or shutil.which("mkisofs") or shutil.which("genisoimage")
    if iso_tool is None:
        raise RuntimeError("Need cloud-localds or xorrisofs/mkisofs/genisoimage in PATH to build cloud-init seed iso")
    await run_command(
        [
            iso_tool,
            "-output",
            str(out),
            "-volid",
            "cidata",
            "-joliet",
            "-rock",
            str(user_data),
            str(meta_data),
        ]
    )


async def _extract_kernel_initrd(
    *,
    imagedir: str,
    ubuntu_name: str,
    os_src_paths: list[str],
    arch: str,
) -> tuple[Path, Path, str]:
    """Pull on-pool kernel + initrd out of a ZFS-rooted packer qcow2, cached by sha256.

    Used on aarch64, where rEFInd -> ZBM -> kexec from the packer image into
    the on-pool kernel panics on EDK2 (see notes/zbm-aarch64-kexec-bug-report.md).
    Spins up a one-shot Ubuntu cloud-image VM that apt-installs zfsutils-linux,
    imports the rpool from the attached packer qcow2(s), and copies the
    highest-versioned vmlinuz + initrd to a 9p host share. Subsequent runs
    cache-hit until the source qcow2 sha256 changes (i.e. packer rebuilt).

    Returns (kernel_path, initrd_path, full_cmdline) where full_cmdline is
    "root=zfs=<bootfs> <org.zfsbootmenu:commandline>" -- composed inside
    the extraction VM by reading the ZBM property off rpool/ROOT, matching
    how ZBM itself builds the kexec cmdline. The caller backfills
    console=/earlycon= defaults if the property doesn't supply them.
    """
    fingerprint = _qcow2_fingerprint([Path(p) for p in os_src_paths])
    cache = Path(imagedir) / "extracted" / fingerprint
    kernel = cache / "kernel"
    initrd = cache / "initrd"
    cmdline_path = cache / "cmdline"

    if kernel.exists() and initrd.exists() and cmdline_path.exists():
        return kernel, initrd, cmdline_path.read_text().strip()

    # Serialise concurrent testrole.py workers extracting the same qcow2:
    # without the lock, parallel testall.py would spin up two extraction VMs
    # on the same fingerprint and race on the shutil.copy2 into cache/.
    cache.parent.mkdir(parents=True, exist_ok=True)
    lock_path = cache.parent / f"{fingerprint}.lock"
    with lock_path.open("w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        # Re-check inside the lock: the previous holder may have just finished
        # extracting; we should cache-hit instead of redoing the work.
        if kernel.exists() and initrd.exists() and cmdline_path.exists():
            return kernel, initrd, cmdline_path.read_text().strip()

        print_line(f"Extracting kernel/initrd from packer qcow2 (cache miss; sha256={fingerprint[:12]})")
        cloud_image = await _ensure_extraction_cloudimg(imagedir, ubuntu_name, arch)

        with tempfile.TemporaryDirectory(dir=imagedir) as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "user-data").write_text(EXTRACTION_USER_DATA)
            (tmp / "meta-data").write_text("instance-id: extract\nlocal-hostname: extract\n")
            seed = tmp / "seed.iso"
            await _build_seed_iso(seed, tmp / "user-data", tmp / "meta-data")

            # Cloud-image overlay (writeable, resized so apt has headroom).
            os_overlay = tmp / "cloud.qcow2"
            await run_command(
                [
                    "qemu-img",
                    "create",
                    "-f",
                    "qcow2",
                    "-b",
                    str(cloud_image),
                    "-F",
                    "qcow2",
                    str(os_overlay),
                    "20G",
                ]
            )

            # Per-disk overlays of the source packer qcow2(s) so import
            # doesn't mutate the originals (and a crashed extraction never
            # corrupts them).
            rpool_overlays: list[str] = []
            for idx, src in enumerate(os_src_paths, start=1):
                overlay = tmp / f"rpool-{idx}.qcow2"
                await run_command(
                    [
                        "qemu-img",
                        "create",
                        "-f",
                        "qcow2",
                        "-b",
                        str(Path(src).resolve()),
                        "-F",
                        "qcow2",
                        str(overlay),
                    ]
                )
                rpool_overlays.append(str(overlay))

            share = tmp / "share"
            share.mkdir()

            accel = "hvf" if platform.system() == "Darwin" else "kvm"
            machine_type = "q35" if arch == "x86_64" else "virt"

            cmd: list[str] = [
                f"qemu-system-{arch}",
                "--drive",
                f"file={os_overlay},if=virtio,format=qcow2,cache=unsafe,discard=unmap",
                "--drive",
                f"file={seed},if=virtio,format=raw",
                *(arg for ov in rpool_overlays for arg in ("--drive", f"file={ov},if=virtio,format=qcow2,cache=unsafe")),
                "-netdev",
                "user,id=net0",
                "-device",
                "virtio-net,netdev=net0",
                "-fsdev",
                f"local,id=share,path={share},security_model=mapped-xattr",
                "-device",
                "virtio-9p-pci,fsdev=share,mount_tag=share",
                "-machine",
                f"type={machine_type},accel={accel}",
                "-cpu",
                "host",
                "-smp",
                "4",
                "-m",
                "2048M",
                "-display",
                "none",
                "-serial",
                "null",
                "-no-reboot",
            ]
            if arch == "aarch64":
                code_path = _uefi_code_path(arch)
                vars_path = tmp / "AAVMF_VARS.fd"
                # Size empty vars from the code blob so pflash pair sizes match.
                await run_command(["truncate", "-s", str(code_path.stat().st_size), str(vars_path)])
                cmd += [
                    "-drive",
                    f"file={code_path},if=pflash,unit=0,format=raw,readonly=on",
                    "-drive",
                    f"file={vars_path},if=pflash,unit=1,format=raw",
                ]

            log_path = tmp / "extract.log"
            print_cmd_line(cmd)
            with log_path.open("wb") as log:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log,
                    stderr=asyncio.subprocess.STDOUT,
                )
                try:
                    # 20 min covers a cold cloud-image apt update + install +
                    # zpool import on a slow link. Cache hits skip all of this.
                    await asyncio.wait_for(proc.wait(), timeout=20 * 60)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
                    raise RuntimeError(f"Kernel extraction timed out (log: {log_path})") from None

            if not (share / "done").exists():
                # Promote the qemu log out of tmpdir so the user can inspect
                # it after TemporaryDirectory cleanup runs.
                fail_log = cache.parent / f"{fingerprint}.failed.log"
                with contextlib.suppress(OSError):
                    shutil.copy2(log_path, fail_log)
                raise RuntimeError(f"Kernel extraction failed; see {fail_log}")

            cache.mkdir(parents=True, exist_ok=True)
            shutil.copy2(share / "kernel", kernel)
            shutil.copy2(share / "initrd", initrd)
            shutil.copy2(share / "cmdline", cmdline_path)

    return kernel, initrd, cmdline_path.read_text().strip()


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
    # Device paths (e.g. ["/dev/vdb", "/dev/vdc"]) for disks attached beyond
    # the OS disk. Populated by prepare() and consumed by run_disk_setup().
    _extra_disk_devices: list[str]
    # Set by prepare() on aarch64 ZFS variants: (kernel, initrd, root_cmdline).
    # _boot_command() emits -kernel/-initrd/-append from this and skips UEFI
    # pflash so the firmware boot chain (rEFInd -> ZBM -> kexec, broken on
    # EDK2+aarch64) is bypassed entirely. None on x86_64 and on minimal.
    _direct_boot: tuple[Path, Path, str] | None

    def __init__(self, machine: str, role: str, keep_vm: bool, ubuntu_name: str, machine_timeout: int, upstream_mirrors: bool = False):
        """QEMU-backed machine wrapper used by integration tests."""
        try:
            spec = QEMU_MACHINE_SPECS[machine]
        except KeyError:
            raise AttributeError(f"Unknown machine: {machine}") from None

        # Mirror PodmanMachine's per-platform image cache: /mnt/qemu on
        # Linux dev hosts, <repo>/packer/artifacts on Mac (matches mise.toml's
        # qemu_dir — /mnt/qemu doesn't exist on Mac).
        system = platform.system()
        if system == "Darwin":
            imagedir = str(Path("packer/artifacts").resolve())
            Path(imagedir).mkdir(parents=True, exist_ok=True)
        elif system == "Linux":
            imagedir = "/mnt/qemu"
        else:
            raise AttributeError(f"Unknown operating system: {system}")

        # ZFS-rooted variants (box / lab / pug) used to fail fast on arm64
        # because the rEFInd -> ZBM -> kexec chain in the packer image panics
        # on EDK2+aarch64. We now extract the on-pool kernel + initrd from
        # the qcow2 once (cached by sha256) and direct-boot via -kernel
        # /-initrd, bypassing the firmware chain. See _extract_kernel_initrd
        # and notes/zbm-aarch64-kexec-bug-report.md.
        self._spec = spec
        super().__init__(
            ssh_port=0,
            ssh_user=spec.ssh_user,
            ansible_args=spec.ansible_args,
            inventory_host=spec.inventory_host,
            idfile="pid",
            imagedir=imagedir,
            machine=machine,
            role=role,
            keep_vm=keep_vm,
            ubuntu_name=ubuntu_name,
            machine_timeout=machine_timeout,
            upstream_mirrors=upstream_mirrors,
        )

    @property
    def host_arch(self) -> str:
        """'x86_64' or 'aarch64' — used for qemu binary, cloud image suffix, etc."""
        m = platform.machine()
        if m in ("x86_64", "amd64"):
            return "x86_64"
        if m in ("aarch64", "arm64"):
            return "aarch64"
        raise RuntimeError(f"Unsupported host architecture: {m}")

    async def prepare(self) -> None:
        """Create overlay images and seed data required for the selected QEMU template."""

        await super().prepare()
        self._extra_disk_devices = []
        self._direct_boot = None

        if self.machine == "minimal":
            if shutil.which("cloud-localds") is None:
                raise RuntimeError("cloud-localds not found in PATH — install cloud-image-utils " "(`apt install cloud-image-utils`) to use the minimal machine.")
            await run_command(
                ["cloud-localds", f"{self.workdir.name}/seed.img", "test/minimal/user-data", "test/minimal/meta-data"],
            )
            cloud_arch = "amd64" if self.host_arch == "x86_64" else "arm64"
            await self._create_overlay(
                f"{self.imagedir}/ubuntu-{self.ubuntu_version}-minimal-cloudimg-{cloud_arch}.img",
                f"{self.workdir.name}/disk.img",
                size="20G",
            )
            self.drives = [
                self._virtio_drive(f"{self.workdir.name}/disk.img"),
                f"file={self.workdir.name}/seed.img,if=virtio,format=raw",
            ]
            # aarch64's `virt` machine boots only via UEFI; x86_64's q35 falls
            # back to SeaBIOS off the same disk, so no flash is needed there.
            if self.host_arch == "aarch64":
                self.drives += await self._uefi_drives()
            return

        # ZFS variants pick a packer image (ubuntu-zfs or ubuntu-zfs-lab),
        # overlay its OS disks, and attach extra empty qcow2s on top for
        # the per-variant disk-setup script to format. See AGENTS.md
        # "Test Environment Design".
        packer_image = self._spec.packer_image
        assert packer_image is not None, f"non-minimal variant {self.machine!r} must declare packer_image"
        image_dir = f"{self.imagedir}/{self.ubuntu_name}/{packer_image}"
        os_disk_count = _PACKER_IMAGE_OS_DISKS[packer_image]

        os_src_paths = [f"{image_dir}/packer-ubuntu-{idx}" for idx in range(1, os_disk_count + 1)]
        os_disk_paths: list[str] = []
        for idx, src in enumerate(os_src_paths, start=1):
            dest = f"{self.workdir.name}/packer-ubuntu-{idx}"
            await self._create_overlay(src, dest)
            os_disk_paths.append(dest)

        # Empty qcow2 disks for the variant's extra pools. test/disks/<variant>.sh
        # partitions / zpool-creates them after the VM boots.
        extra_paths: list[str] = []
        for idx, size in enumerate(self._spec.extra_disks, start=os_disk_count + 1):
            path = f"{self.workdir.name}/packer-ubuntu-{idx}"
            await run_command(["qemu-img", "create", "-f", "qcow2", path, size])
            extra_paths.append(path)

        # OS disks take vda..vd[a+N-1]; extras attach right after, in spec order.
        self._extra_disk_devices = [f"/dev/vd{chr(ord('a') + os_disk_count + i)}" for i in range(len(extra_paths))]

        self.drives = [
            *(self._virtio_drive(path) for path in os_disk_paths),
            *(self._virtio_drive(path) for path in extra_paths),
        ]
        if self.host_arch == "x86_64":
            await self._copy_efivars_from(image_dir)
            self.drives += await self._uefi_drives()
        else:
            # aarch64: bypass the firmware boot chain (rEFInd -> ZBM -> kexec
            # panics on EDK2) by extracting the on-pool kernel + initrd from
            # the packer qcow2 once and direct-booting them. Cached by content
            # sha256 so a packer rebuild auto-invalidates.
            self._direct_boot = await _extract_kernel_initrd(
                imagedir=self.imagedir,
                ubuntu_name=self.ubuntu_name,
                os_src_paths=os_src_paths,
                arch=self.host_arch,
            )

    async def run_disk_setup(self) -> None:
        """Push test/disks/<variant>.sh to the VM and run it via sudo.

        Receives the extra disk devices as positional args. The script is
        idempotent (skips already-existing pools) so --keep re-runs work.
        """
        script_path = self._spec.disk_setup_script
        if not script_path or not self._extra_disk_devices:
            return
        remote_path = "/tmp/disk_setup.sh"
        await run_command(self.format_scp_cmd(script_path, remote_path))
        await self.ssh_command("sudo", "bash", remote_path, *self._extra_disk_devices)

    async def _create_overlay(self, src: str, dest: str, size: str | None = None) -> None:
        """Create a qcow2 overlay pointing at *src* with optional resize."""

        args = ["qemu-img", "create", "-f", "qcow2", "-b", src, "-F", "qcow2", dest]
        if size:
            args.append(size)
        await run_command(args)

    async def _copy_efivars_from(self, image_dir: str) -> None:
        """Copy EFI vars for UEFI boots from *image_dir* into the workdir."""

        await run_command(
            ["cp", f"{image_dir}/efivars.fd", f"{self.workdir.name}/efivars.fd"],
        )

    def _virtio_drive(self, path: str) -> str:
        """Return a virtio drive string with sensible cache/discard flags."""

        return f"file={path},if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap"

    async def _uefi_drives(self) -> list[str]:
        """UEFI pflash code+vars pair for the host arch.

        The CODE blob is resolved per-arch via _uefi_code_path (Homebrew on
        macOS, ovmf/qemu-efi-aarch64 on Linux). The VARS blob is one of:

        - {workdir}/efivars.fd, if it's been copied in via _copy_efivars_from
          (ZFS variants -- the packer image ships a primed efivars template
          so the bootloader entries survive across runs).
        - else a fresh empty 64 MiB file -- right for ad-hoc launches like
          minimal or launch.py --with-pflash where there's no prior state.
        """
        code_path = _uefi_code_path(self.host_arch)
        packer_vars = Path(f"{self.workdir.name}/efivars.fd")
        if packer_vars.exists():
            vars_path = packer_vars
        else:
            vars_path = Path(f"{self.workdir.name}/uefi-vars.fd")
            # qemu pflash requires CODE and VARS to be the same size, and
            # EDK2 builds aren't uniform: aarch64 EDK2 ships at 64 MiB,
            # x86_64 OVMF typically at 4 MiB. Size the empty vars from the
            # code blob so the pflash pair lines up regardless of arch /
            # distro.
            code_size = code_path.stat().st_size
            await run_command(["truncate", "-s", str(code_size), str(vars_path)])
        return [
            f"file={code_path},if=pflash,unit=0,format=raw,readonly=on",
            f"file={vars_path},if=pflash,unit=1,format=raw",
        ]

    def _boot_command(self) -> list[str]:
        """Assemble the qemu command line for the prepared disks.

        Arch- and OS-aware: qemu-system-{x86_64,aarch64} per host arch,
        accel=kvm on Linux / hvf on Mac, machine type q35 (x86_64) or virt
        (aarch64). Display hardware (virtio-gpu-pci + qemu-xhci) works
        identically on both arches.
        """
        accel = "hvf" if platform.system() == "Darwin" else "kvm"
        machine_type = "q35" if self.host_arch == "x86_64" else "virt"

        if self.keep_vm:
            # q35 already has std VGA, PS/2 keyboard, and ICH9 USB controllers
            # by default, so we only add usb-tablet there (absolute-coordinate
            # mouse for VNC). aarch64 virt has no default graphics or input
            # devices, so it needs the full virtio-gpu + xhci + usb-kbd set.
            if self.host_arch == "aarch64":
                arch_devices = ["-device", "virtio-gpu-pci", "-device", "qemu-xhci", "-device", "usb-kbd", "-device", "usb-tablet"]
            else:
                arch_devices = ["-device", "usb-tablet"]
            display_args = ["-display", "vnc=:0,to=99", *arch_devices, "-k", "fr"]
        else:
            display_args = ["-display", "none"]

        direct_boot: list[str] = []
        if self._direct_boot is not None:
            kernel, initrd, cmdline = self._direct_boot
            # cmdline is composed in extraction as
            # "root=zfs=<bootfs> <org.zfsbootmenu:commandline>" -- the ZBM
            # property is the canonical place to set per-pool boot args, so
            # we honour it verbatim. If the cmdline doesn't already wire up
            # this arch's serial UART we backfill defaults so qemu's
            # `-serial stdio` receives kernel printk for the boot log.
            # qemu virt aarch64 exposes a PL011 at 0x9000000 (-> ttyAMA0);
            # qemu q35 x86_64 exposes a 16550-compatible UART at I/O 0x3f8
            # (-> ttyS0). Match by ttyAMA/ttyS so a property that already
            # configures the right console doesn't get a duplicate appended.
            # Order matters: Linux makes the LAST `console=` the primary
            # /dev/console. We want serial primary (so ZBM TUI / login prompts
            # land on -serial stdio in --foreground mode) and tty0 just
            # secondary so VNC also gets kernel printk. Append tty0 first,
            # then the arch-specific serial console.
            extras: list[str] = []
            if self.keep_vm and "console=tty0" not in cmdline:
                # virtio-gpu-pci is attached when keep_vm=True, giving fbcon
                # something to bind to. Skipped headless -- without a graphics
                # device tty0 has nothing to render onto.
                extras.append("console=tty0")
            if self.host_arch == "aarch64":
                if "console=ttyAMA" not in cmdline:
                    extras.append("console=ttyAMA0,115200 earlycon=pl011,0x9000000,115200")
            else:
                if "console=ttyS" not in cmdline:
                    extras.append("console=ttyS0,115200 earlycon=uart8250,io,0x3f8,115200")
            if extras:
                cmdline = f"{cmdline} {' '.join(extras)}"
            direct_boot = ["-kernel", str(kernel), "-initrd", str(initrd), "-append", cmdline]

        return [
            "timeout",
            "--kill-after=10s",
            str(self.wrapper_timeout),
            f"qemu-system-{self.host_arch}",
            *[arg for drive in self.drives for arg in ("--drive", drive)],
            *direct_boot,
            "-netdev",
            f"user,id=user.0,hostfwd=tcp:{self.ssh_host}:0-:22",
            "-object",
            "rng-random,id=rng0,filename=/dev/urandom",
            "-device",
            "virtio-rng-pci,rng=rng0",
            "-machine",
            f"type={machine_type},accel={accel}",
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
        raise RuntimeError(f"Unable to determine SSH port from qemu lsof output (pid {pid}):\n{lsof_dump}")


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
        ["podman", "image", "inspect", "--format", '{{ index .Config.Labels "' + BAKE_HASH_LABEL + '" }}', tag],
        check=False,
        quiet=True,
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
            imagedir = str(Path("packer/artifacts").resolve())
            Path(imagedir).mkdir(parents=True, exist_ok=True)
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
                        ["podman", "commit", "--change", f"LABEL homelab.bake-hash={bake_hash}", cid, self.commit_image],
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
