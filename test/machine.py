#!/usr/bin/env -S uv run

import asyncio
import collections
import contextlib
import dataclasses
import platform
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import ClassVar, NamedTuple, Self

from arch import ArchProfile, detect_host_arch, uefi_code_path_for
from setup_mitogen import ensure_mitogen_symlink
from utils import (
    CommandResult,
    IdempotenceFailedException,
    build_seed_iso,
    print_cmd_line,
    print_line,
    read_and_write_stream,
    run_command,
    sleep_tick,
    terminate_pid,
)

OUT_DIR = Path("test/out")
# Created once at import so every later writer (per-run logs, etc.) can
# open files inside without repeating the mkdir.
OUT_DIR.mkdir(parents=True, exist_ok=True)

UBUNTU_RELEASES: dict[str, str] = {
    "jammy": "22.04",
    "noble": "24.04",
    "resolute": "26.04",
}
DEFAULT_UBUNTU = "jammy"
SSH_KEY = "packer/vagrant.key"
SSH_HOST = "127.0.0.1"


class QemuMachineSpec(NamedTuple):
    ssh_user: str
    inventory_host: str
    # Packer image directory under /mnt/scratch/qemu/<ubuntu_name>/. None means the
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
    # Number of qcow2 disks the packer image stages as part of the OS install.
    # zfs is single-rpool (1), zfs-lab is a 3-disk mirror rpool
    # (3). prepare() overlays packer-ubuntu-1..N for the OS, then attaches
    # the variant's extra_disks starting at vd[a+N]. Unused on minimal
    # (packer_image=None), where the cloud-image branch returns early.
    os_disk_count: int = 0
    # True for the cloud-image-only "minimal" variant. Toggles
    # qemu_test_minimal in the role's ansible vars and picks the
    # `<inventory_host>-qemu-minimal.yml` host_vars file instead of the
    # default `<inventory_host>-qemu.yml`.
    qemu_test_minimal: bool = False
    # Guest RAM in MiB and vcpu count, plumbed into qemu's -m / -smp.
    # Defaults match the historical 4096M / 8-vcpu sizing so existing
    # variants are unchanged; minimal trims to 2048/4 since the cloud-
    # image variant has no zpool to feed. -smp emits a single-socket
    # layout (sockets=1,cores=vcpus), the conventional shape for a guest
    # on a non-NUMA hypervisor.
    memory_mb: int = 4096
    vcpus: int = 8


QEMU_MACHINE_SPECS: dict[str, QemuMachineSpec] = {
    "minimal": QemuMachineSpec(
        ssh_user="ubuntu",
        inventory_host="box",
        packer_image=None,
        extra_disks=[],
        disk_setup_script=None,
        qemu_test_minimal=True,
        memory_mb=2048,
        vcpus=4,
    ),
    "box": QemuMachineSpec(
        ssh_user="vagrant",
        inventory_host="box",
        packer_image="zfs",
        extra_disks=[],
        disk_setup_script=None,
        os_disk_count=1,
    ),
    "lab": QemuMachineSpec(
        ssh_user="vagrant",
        inventory_host="lab",
        # zfs-lab brings the 3-disk mirror rpool baked in (matches
        # the lab-class prod host). Extras below add the dozer/tank/mouse
        # disks layered on top by test/disks/lab.sh.
        packer_image="zfs-lab",
        # Six disks: dozer mirror legs (1G ×2), tank/mouse shared hosts
        # (1.5G ×2 — partitioned at test time), plus two whole-disk tank
        # raidz2 vdevs (1G ×2). Sizes match the previous packer-baked layout.
        extra_disks=["1G", "1G", "1.5G", "1.5G", "1G", "1G"],
        disk_setup_script="test/disks/lab.sh",
        os_disk_count=3,
    ),
    "pug": QemuMachineSpec(
        ssh_user="vagrant",
        inventory_host="pug",
        packer_image="zfs",
        extra_disks=["1G", "1G"],
        disk_setup_script="test/disks/pug.sh",
        os_disk_count=1,
    ),
}


MACHINE_CHOICES: tuple[str, ...] = tuple(QEMU_MACHINE_SPECS)


def _qemu_ansible_args(spec: QemuMachineSpec) -> list[str]:
    """Derive the per-spec -e args for ansible-playbook.

    Two -e bundles: a JSON blob toggling qemu_test / qemu_test_minimal,
    and a host_vars file matching the inventory host. Centralising the
    derivation keeps QEMU_MACHINE_SPECS a pure data table.
    """
    suffix = "-minimal" if spec.qemu_test_minimal else ""
    return [
        "-e",
        f'{{"qemu_test":true,"qemu_test_minimal":{str(spec.qemu_test_minimal).lower()}}}',
        "-e",
        f"@host_vars/{spec.inventory_host}-qemu{suffix}.yml",
    ]


SSH_WAIT_TIMEOUT = 120
IDFILE_TIMEOUT = 60

# Sentinel printed by testrole.py at end-of-run so testall.py can capture
# the per-machine peak RSS via stdout. Kept simple on purpose: a single
# `key=int` line is trivial to parse and unlikely to collide with the
# free-form output ansible/qemu emit upstream.
PEAK_KB_SENTINEL_PREFIX = "PEAK_KB="


@dataclasses.dataclass
class Machine:
    """Base runner that wraps a test target reachable over SSH and Ansible."""

    # Extra seconds added on top of machine_timeout for the GNU `timeout` /
    # `podman --timeout` last-resort wrapper. Has to outlast the inner
    # asyncio.timeout in run_test so testrole.py's own deadline fires first
    # (and we get a clean rc=124 + stop()), with enough headroom for
    # Machine.stop() to do its graceful->SIGKILL escalation. ClassVar so
    # @dataclass doesn't promote it to an init field.
    WRAPPER_GRACE_SECONDS: ClassVar[int] = 60

    ssh_port: int
    ssh_user: str
    ansible_args: list[str]
    inventory_host: str
    machine: str
    role: str
    keep_vm: bool
    ubuntu_name: str
    machine_timeout: int
    upstream_mirrors: bool = False
    # Optional workdir parent override. When None, _workdir_parent() falls
    # through to the subclass default (system tmp for the base class, the
    # imagedir for QemuMachine). Wired by testrole.py from --workdir-parent
    # / $HOMELAB_WORKDIR_PARENT so CI workflows can keep the qcow2 tree
    # mounted ro and stage the per-run TempDir somewhere ephemeral.
    workdir_parent: Path | None = None
    # Filename (under the per-run workdir) where qemu writes its pidfile.
    idfile: str = dataclasses.field(default="pid", init=False)

    proc: asyncio.subprocess.Process | None = dataclasses.field(default=None, init=False)
    output_file: Path = dataclasses.field(init=False)
    journal_file: Path = dataclasses.field(init=False)
    boot_file: Path = dataclasses.field(init=False)
    dmesg_file: Path = dataclasses.field(init=False)
    systemctl_failed_file: Path = dataclasses.field(init=False)
    workdir: tempfile.TemporaryDirectory[str] = dataclasses.field(init=False)
    peak_rss_kb: int = dataclasses.field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.ubuntu_name not in UBUNTU_RELEASES:
            raise ValueError(f"Unknown Ubuntu release '{self.ubuntu_name}'; known: {sorted(UBUNTU_RELEASES)}")
        # Refresh the .ansible-mitogen-strategy symlink before we run any
        # ansible-playbook subprocess. Lifted out of module-import time so
        # bare imports (tests reading constants, tooling) skip the work;
        # constructing a Machine implies we're about to drive ansible.
        ensure_mitogen_symlink()
        prefix = f"{self.machine}.{self.ubuntu_name}.{self.role}"
        self.output_file = OUT_DIR / f"{prefix}.output.ansi"
        self.journal_file = OUT_DIR / f"{prefix}.journal.ansi"
        self.boot_file = OUT_DIR / f"{prefix}.boot.ansi"
        self.dmesg_file = OUT_DIR / f"{prefix}.dmesg.ansi"
        self.systemctl_failed_file = OUT_DIR / f"{prefix}.systemctl-failed.ansi"
        # output/boot get truncated by their respective open("w") at write
        # time, so they don't need pre-cleanup. The failure-only artifacts
        # (journal/dmesg/systemctl-failed) are only ever created on a
        # failed run, so stale ones from a prior run would otherwise
        # survive into a healthy run -- drop them explicitly.
        for stale in (self.journal_file, self.dmesg_file, self.systemctl_failed_file):
            stale.unlink(missing_ok=True)
        # System tmp by default; subclasses that need the workdir on a
        # specific filesystem (qemu's qcow2 cache) override _workdir_parent().
        # Auto-create the parent so --workdir-parent /some/new/path just
        # works without callers having to mkdir -p first; tempfile itself
        # doesn't create the dir argument, only the per-run subdir under it.
        wd_parent = self._workdir_parent()
        if wd_parent is not None:
            Path(wd_parent).mkdir(parents=True, exist_ok=True)
        self.workdir = tempfile.TemporaryDirectory(dir=wd_parent)
        self._preflight()

    def _workdir_parent(self) -> Path | None:
        """Return the parent directory for the per-run TemporaryDirectory.

        Default None puts the workdir under the system tmp. QemuMachine
        overrides to put it on the same filesystem as the packer qcow2s
        so qemu-img overlays don't cross device boundaries. An explicit
        workdir_parent at construction time overrides both — that's the
        CI knob.
        """
        return self.workdir_parent

    def _preflight(self) -> None:
        """Validate external dependencies; subclasses override with tool checks.

        Called once at the end of __post_init__, after self.workdir exists,
        so the failure surface (binary checks, image cache lookups, etc.) is
        bounded to "things the harness will need before the next subprocess
        spawn". Failures raise RuntimeError with installation guidance.
        """
        return

    @staticmethod
    def _require_binary(name: str, hint: str) -> None:
        if shutil.which(name) is None:
            raise RuntimeError(f"Required binary {name!r} not found on PATH. {hint}")

    @property
    def ubuntu_version(self) -> str:
        """Numeric version (e.g. "22.04") for the configured release."""
        return UBUNTU_RELEASES[self.ubuntu_name]

    @property
    def wrapper_timeout(self) -> int:
        """Last-resort timeout passed to coreutils `timeout` / podman --timeout.

        0 disables the wrapper (`timeout 0` runs forever, podman --timeout 0
        is "no timeout") so an interactive --keep session isn't cut short.
        Otherwise it's machine_timeout (the Python deadline) plus a small
        grace window so Machine.stop() finishes its graceful->SIGKILL
        escalation before the wrapper kills its child.
        """
        if self.keep_vm:
            return 0
        return self.machine_timeout + self.WRAPPER_GRACE_SECONDS

    def _ssh_options(self) -> list[str]:
        """Return the shared `-o flag=value` pairs for ssh and scp."""
        return [
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
        ]

    def format_ssh_cmd(self, *cmd: str) -> list[str]:
        """Return an ssh invocation pinned to this instance."""

        # ForwardAgent=yes on every connection (ssh and scp) so whichever
        # one happens to create the ControlMaster (per the user's
        # ~/.ssh/config Host *: ControlMaster auto) seeds it with an
        # agent-forwarding channel. Otherwise ansible's later
        # ForwardAgent=yes silently reuses the agent-less master and
        # breaks roles that ssh out to git@github.com from the target
        # (compta on lab: run_disk_setup's scp wins the race over the
        # mirrors playbook's ssh and the master comes up agent-less).
        base = [
            "ssh",
            "-i",
            SSH_KEY,
            "-p",
            str(self.ssh_port),
            *self._ssh_options(),
            "-o",
            "ForwardAgent=yes",
            f"{self.ssh_user}@{SSH_HOST}",
        ]
        return [*base, shlex.join(cmd)] if cmd else base

    def format_scp_cmd(self, local: str, remote: str) -> list[str]:
        """Return an scp invocation pinned to this instance.

        Shares `_ssh_options()` with `format_ssh_cmd`; only the port flag
        differs (scp uses `-P`, ssh uses `-p`). ForwardAgent matches ssh
        so a master created by scp carries the agent channel — see the
        block comment in `format_ssh_cmd` for why this matters.
        """
        return [
            "scp",
            "-i",
            SSH_KEY,
            "-P",
            str(self.ssh_port),
            *self._ssh_options(),
            "-o",
            "ForwardAgent=yes",
            local,
            f"{self.ssh_user}@{SSH_HOST}:{remote}",
        ]

    def ansible_env(self) -> dict[str, str]:
        """ANSIBLE_* environment overrides layered on top of os.environ.

        Fact cache lives inside the per-run workdir, so the ~9
        ansible-playbook invocations in one test share gathered facts
        (saves ~0.9s per replay) without leaking facts across runs that
        target a freshly-spawned host (different IP, different cgroup,
        different filesystem).
        """
        return {
            "ANSIBLE_DISPLAY_OK_HOSTS": "true",
            "ANSIBLE_DISPLAY_SKIPPED_HOSTS": "true",
            "ANSIBLE_GATHERING": "smart",
            "ANSIBLE_FACT_CACHING": "jsonfile",
            "ANSIBLE_FACT_CACHING_CONNECTION": f"{self.workdir.name}/facts",
            "ANSIBLE_FACT_CACHING_TIMEOUT": "7200",
        }

    def format_ansible_cmd(self, *cmd: str) -> list[str]:
        """Build an ansible-playbook command pinned to this machine's SSH details.

        ANSIBLE_* env vars come back from ansible_env() and are passed to
        run_command via env=, not prepended to argv.
        """
        parts = [
            "ansible-playbook",
            "-e",
            f"ansible_ssh_port={self.ssh_port}",
            "-e",
            f"ansible_ssh_host={SSH_HOST}",
            "-e",
            f"ansible_ssh_user={self.ssh_user}",
            "-e",
            f"ansible_ssh_private_key_file={SSH_KEY}",
            # Static playbooks declare `hosts: all`; --limit pins the play to
            # the inventory host we actually provisioned.
            "--limit",
            self.inventory_host,
            # Static playbooks reference `_role_under_test` for `import_role`
            # so site.yml / _setup.yml / _verify.yml are all role-agnostic
            # on disk.
            "-e",
            f"_role_under_test={self.role}",
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

        return await run_command(self.format_ansible_cmd(*cmd), check=check, env=self.ansible_env())

    async def run_disk_setup(self) -> None:
        """Run the variant's post-boot disk-setup script over SSH, if any.

        Default no-op for machines that don't carry extra disks (container,
        minimal, box). QemuMachine overrides for variants whose spec sets a
        disk_setup_script (lab, pug).
        """
        return

    async def prepare(self) -> None:
        """Stage a temporary workdir with inventory snippets and the static playbooks."""

        Path("group_vars").copy_into(self.workdir.name)
        Path("host_vars").copy_into(self.workdir.name)
        Path("wireguard").copy_into(self.workdir.name)
        Path("roles").copy_into(self.workdir.name)

        # mise.toml + uv lock + pyproject are repo-root files that some
        # roles reference via `{{ playbook_dir }}/<file>` (e.g. act_runner
        # bakes them into its lab-runtime container build context).
        # Stage them so the harness's workdir mirrors what ansible sees
        # on a production controller run.
        for repo_root_file in ("mise.toml", "pyproject.toml", "uv.lock"):
            src = Path(repo_root_file)
            if src.exists():
                src.copy_into(self.workdir.name)

        # Copy the static role-agnostic playbooks (site / _setup / _test /
        # _verify / _mirrors) into the workdir so ansible loads
        # group_vars/host_vars from this directory and the playbooks
        # reference `{{ _role_under_test }}` injected via -e in
        # format_ansible_cmd. testrole.py decides which hook playbook to
        # invoke by checking roles/<role>/tasks/<hook>.yml at the source.
        for playbook in Path("test/playbooks").glob("*.yml"):
            playbook.copy_into(self.workdir.name)

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
                asyncio.open_connection(SSH_HOST, self.ssh_port),
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
            # wait_closed() returns immediately for a healthy transport;
            # OSError catches the case where the peer dropped the connection
            # before our close completed. We don't time-bound it -- if
            # close ever genuinely hangs that's a real bug worth surfacing.
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _collect_remote_to_file(self, label: str, dest: Path, *remote_cmd: str) -> bool:
        """Run *remote_cmd* over SSH, capture stdout into *dest*.

        Returns True on remote-exit-zero, False otherwise. stderr is streamed
        to the main log; stdout goes only to *dest*. *label* is what we print
        when the capture fails or succeeds. Used by the per-run failure
        diagnostics so each artifact (journal, dmesg, systemctl --failed) is
        a separate file the operator (or CI artifact upload) can read in
        isolation.
        """
        cmd = self.format_ssh_cmd(*remote_cmd)
        print_cmd_line(cmd)

        with dest.open("w") as handle:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=handle,
                stderr=asyncio.subprocess.PIPE,
            )
            assert proc.stderr is not None
            # stderr only -- stdout is already going to the file. Drain to
            # EOF before waiting so a chatty stderr can't deadlock the
            # child by filling the pipe buffer. Ordering: stdout lines land
            # in *dest* in source order (single FD, kernel FIFO); stderr
            # lines land in the main log in source order. The two streams
            # go to different destinations so there's no cross-stream
            # interleave to worry about here.
            await read_and_write_stream(proc.stderr, "red", [])
            exitcode = await proc.wait()

        if exitcode != 0:
            print_line(f"Failed to collect {label}: exit code {exitcode}")
            return False
        print_line(f"{label}: {dest}")
        return True

    async def collect_failure_artifacts(self) -> None:
        """Collect post-mortem diagnostics from the guest after a failed run.

        Three artifacts:
          - <variant>.<role>.journal.ansi -- full systemd journal
          - <variant>.<role>.dmesg.ansi -- guest kernel ring buffer
          - <variant>.<role>.systemctl-failed.ansi -- list of failed units

        Each runs as a best-effort capture: a failure of one doesn't shadow
        the others. The caller (testrole.py) then tails just the journal
        for in-terminal context; the rest are downloaded as CI artifacts
        when needed.
        """

        await self._collect_remote_to_file(
            "Systemd journal",
            self.journal_file,
            "env",
            "SYSTEMD_COLORS=true",
            "journalctl",
            "--no-pager",
            "--priority",
            "info",
        )
        await self._collect_remote_to_file(
            "Kernel ring buffer",
            self.dmesg_file,
            "sudo",
            "dmesg",
            "--color=always",
            "--ctime",
        )
        await self._collect_remote_to_file(
            "Failed units",
            self.systemctl_failed_file,
            "env",
            "SYSTEMD_COLORS=true",
            "systemctl",
            "--failed",
            "--no-pager",
        )

    def cleanup_logs(self) -> None:
        """Remove all per-run log artifacts."""
        for path in (
            self.output_file,
            self.boot_file,
            self.journal_file,
            self.dmesg_file,
            self.systemctl_failed_file,
        ):
            path.unlink(missing_ok=True)

    def print_file_tail(self, path: Path, n: int = 50) -> None:
        """Print the last *n* lines of *path* to stdout, no-op if missing."""
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

    async def __aexit__(self, exc_type: object, exc: BaseException | None, tb: object) -> None:
        print_line("Stopping machine...")
        await self.stop()
        # Surface the tail of the boot/console log on infra-shaped failures so
        # the main transcript ends with the most likely diagnostic. Cancellation
        # is the user wanting out; idempotence checks fail at the role layer
        # and the boot log won't help.
        if exc is not None and not isinstance(exc, (asyncio.CancelledError, IdempotenceFailedException)):
            self.print_file_tail(self.boot_file)

    async def stop(self) -> None:
        """Drain the boot subprocess and free temp resources.

        Subclasses perform hypervisor-specific cleanup (qemu kill, podman rm)
        before delegating here, so the inner child is already dead by the
        time we get here. The wrapper (`timeout` for qemu, `podman run`
        client for podman) should notice and exit on its own immediately --
        we just wait for it. SIGKILL after 5s in case something pathological
        keeps it alive (zombie subprocess, hung pipe).
        """
        try:
            if self.proc and self.proc.returncode is None:
                try:
                    async with asyncio.timeout(5):
                        await self.proc.wait()
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        self.proc.kill()
                    await self.proc.wait()
        finally:
            self.workdir.cleanup()


def imagedir_for_host() -> Path:
    """Return the platform's packer-image cache root.

    /mnt/scratch/qemu on Linux dev hosts; <repo>/packer/artifacts on Mac
    (matches mise.toml's qemu_dir; /mnt/scratch/qemu doesn't exist on Mac).
    Linux raises if the mountpoint is missing -- the dev host workflow
    expects the qemu volume to be mounted before any test runs.
    """
    system = platform.system()
    if system == "Darwin":
        d = Path("packer/artifacts").resolve()
        d.mkdir(parents=True, exist_ok=True)
        return d
    if system == "Linux":
        d = Path("/mnt/scratch/qemu")
        if not d.is_dir():
            raise RuntimeError(f"Imagedir {str(d)!r} does not exist. " f"Mount the qemu image volume (e.g. `sudo mount /mnt/scratch/qemu`).")
        return d
    raise RuntimeError(f"Unknown operating system: {system}")


def sweep_stale_workdirs(imagedir: Path) -> None:
    """Reap orphaned tmp* (harness) and .build-* (packer) dirs from prior runs.

    Cleanup normally rides Machine.__aexit__'s finally chain (machine.py:579)
    for tmp* and the trailing rmdir in mise-tasks/packer/build for .build-*.
    Both bypass on SIGKILL / OOM / power-loss, leaving orphan dirs. Each is a
    full repo copy plus a qcow2 overlay -- ansible-lint also walks into them
    until .ansible-lint excludes the path, so leaks are doubly expensive.

    Liveness check: a single `ps -Ao args=` covers every running process; a
    candidate is reaped only if its dirname appears nowhere in argv AND its
    mtime is older than the race-window grace. The grace protects the brief
    interval between QemuMachine.__post_init__ creating the workdir and qemu
    starting (and thus showing up in ps), so concurrent harness runs don't
    nuke each other's freshly-minted workdirs.
    """
    if not imagedir.is_dir():
        return

    grace_seconds = 60
    now = time.time()
    candidates = [d for d in imagedir.iterdir() if d.is_dir() and (d.name.startswith("tmp") or d.name.startswith(".build-"))]
    if not candidates:
        return

    try:
        ps_args = subprocess.run(["ps", "-Ao", "args="], check=True, capture_output=True, text=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Don't reap when we can't verify staleness.
        return

    for d in candidates:
        try:
            age = now - d.stat().st_mtime
        except OSError:
            continue
        if age < grace_seconds:
            continue
        if d.name in ps_args:
            continue
        print_line(f"Reaping orphaned workdir {d}")
        shutil.rmtree(d, ignore_errors=True)


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
    # VNC display number (0..99) chosen in prepare() when keep_vm is True;
    # consumed by _boot_command for the `-display vnc=` argument. Bound on
    # 5900+display so qemu won't try to walk the band itself.
    vnc_display: int
    # Set by prepare() on aarch64 ZFS variants: (kernel, initrd, root_cmdline).
    # _boot_command() emits -kernel/-initrd/-append from this and skips UEFI
    # pflash so the firmware boot chain (rEFInd -> ZBM -> kexec, broken on
    # EDK2+aarch64) is bypassed entirely. None on x86_64 and on minimal.
    _direct_boot: tuple[Path, Path, str] | None

    def __init__(
        self,
        machine: str,
        role: str,
        keep_vm: bool,
        ubuntu_name: str,
        machine_timeout: int,
        upstream_mirrors: bool = False,
        *,
        workdir_parent: Path | None = None,
        image_dir: Path | None = None,
        kernel: Path | None = None,
        initrd: Path | None = None,
        append: str = "",
        mem: str | None = None,
        with_pflash: bool = False,
        efi_code: Path | None = None,
        efi_vars: Path | None = None,
        virtfs: list[tuple[Path, str]] | None = None,
        foreground: bool = False,
        qmp_socket: Path | None = None,
    ):
        """QEMU-backed machine wrapper used by integration tests.

        image_dir overrides the per-variant `<imagedir>/<ubuntu>/<packer_image>`
        path that prepare() would otherwise compute. Used by qemu.pkr.hcl's
        verify-boot post-processor to point the harness at a still-staged
        `*.new` build output before the build script swaps it over the
        previous artifact.

        kernel/initrd/append direct-boot a user-supplied kernel against the
        variant's qcow2, replacing the packer-shipped kernel/initrd on the
        aarch64 ZFS path (or layering -kernel/-initrd onto x86_64). mem
        overrides the per-spec `-m` size. with_pflash / efi_code / efi_vars
        attach UEFI pflash on variants that don't get it from prepare()
        (auto-detected paths or explicit overrides). virtfs is a list of
        (host_path, mount_tag) 9p shares. foreground strips the `timeout`
        wrapper and switches `-serial stdio` -> mon:stdio so HMP is reachable
        via Ctrl-A,c. qmp_socket binds qemu's QMP server to a unix socket.
        These last set are launch.py-only knobs; testrole.py / production
        callers leave them at defaults.
        """
        try:
            spec = QEMU_MACHINE_SPECS[machine]
        except KeyError:
            raise AttributeError(f"Unknown machine: {machine}") from None

        # Imagedir layout is platform-gated (see imagedir_for_host); the disk
        # format alongside it is too: raw on Linux (the host ZFS dataset already
        # does CoW + zstd), qcow2 on Mac (APFS has no fs-level compression).
        # Mirrors qemu.pkr.hcl's image_format var.
        self.imagedir: Path = imagedir_for_host()
        self._packer_disk_format = "qcow2" if platform.system() == "Darwin" else "raw"

        self._spec = spec
        if image_dir is not None and spec.packer_image is None:
            raise ValueError(f"image_dir override requires a variant with packer_image set, got {machine!r}")
        self._image_dir_override = image_dir.resolve() if image_dir is not None else None
        # launch.py-only knobs; defaults are no-ops (every gate below the
        # super().__init__ call short-circuits when each is None/False/[]).
        self._direct_boot_override: tuple[Path, Path, str] | None = (kernel.resolve(), initrd.resolve(), append) if kernel is not None else None
        self._mem = mem
        self._with_pflash = with_pflash
        self._efi_code = efi_code.resolve() if efi_code is not None else None
        self._efi_vars = efi_vars.resolve() if efi_vars is not None else None
        self._virtfs: list[tuple[Path, str]] = list(virtfs or [])
        self._foreground = foreground
        self._qmp_socket = qmp_socket
        # Captured once at construction so prepare()/_boot_command() don't
        # have to re-run platform.machine() on every access.
        self.arch: ArchProfile = detect_host_arch()
        super().__init__(
            ssh_port=0,
            ssh_user=spec.ssh_user,
            ansible_args=_qemu_ansible_args(spec),
            inventory_host=spec.inventory_host,
            machine=machine,
            role=role,
            keep_vm=keep_vm,
            ubuntu_name=ubuntu_name,
            machine_timeout=machine_timeout,
            upstream_mirrors=upstream_mirrors,
            workdir_parent=workdir_parent,
        )
        # idfile defaults to "pid" on the base, which is what we want here.

    def _workdir_parent(self) -> Path | None:
        """Place the workdir alongside the packer qcow2s.

        qemu-img backing-file overlays reach the source qcow2 by absolute
        path, so the workdir doesn't strictly need to be colocated, but
        keeping it on the same filesystem matches the operator's mental
        model and keeps `du` totals predictable. When the caller supplies
        an explicit workdir_parent (CI flag) we honour that instead so
        the imagedir can be ro-mounted.
        """
        return self.workdir_parent or self.imagedir

    def print_ssh_instructions(self) -> None:
        super().print_ssh_instructions()
        # vnc_display is only set when keep_vm=True; print_ssh_instructions
        # itself is also keep-only, so we always have a display here.
        print_line(f"VNC: 127.0.0.1:{5900 + self.vnc_display}")

    @staticmethod
    def _pick_vnc_display() -> int:
        """Walk VNC ports 5900..5999 and return the first free display number.

        qemu's `vnc=:N` syntax binds to port 5900+N, so we test bind on the
        actual port and hand qemu the matching display. Mirrors qemu's own
        `to=99` walk but resolves up front so we know the chosen display
        before launch (and can print it). Raises if all 100 are occupied.
        """
        for display in range(100):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind((SSH_HOST, 5900 + display))
                    return display
            except OSError:
                continue
        raise RuntimeError("No free VNC display in 0..99 on 127.0.0.1")

    def _preflight(self) -> None:
        """Verify the qemu binary, GNU timeout, and lsof are reachable."""
        self._require_binary(
            self.arch.qemu_binary,
            "Install via `brew install qemu` (macOS) " f"or `apt install qemu-system-{self.arch.name}` (Debian/Ubuntu).",
        )
        # The boot wrapper uses GNU timeout; macOS doesn't ship one out of
        # the box, but `brew install coreutils` puts a `timeout` shim on PATH.
        self._require_binary(
            "timeout",
            "Install via `brew install coreutils` (macOS) or via the coreutils " "package on Linux.",
        )

    async def prepare(self) -> None:
        """Create overlay images and seed data required for the selected QEMU template."""

        await super().prepare()
        self._extra_disk_devices = []
        self._direct_boot = None

        # Pre-pick a free TCP port on 127.0.0.1 and pin qemu's hostfwd to it.
        # Replaces the prior lsof-poll heuristic (which had to filter VNC ports
        # and would re-tangle if any future qemu service published TCP). Tiny
        # race window between close() and qemu's bind, but qemu launches
        # near-immediately and the kernel rarely reissues a freshly-released
        # port within microseconds.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((SSH_HOST, 0))
            self.ssh_port = s.getsockname()[1]

        if self.keep_vm:
            # Pick a VNC display 0..99 the same way -- qemu's vnc= syntax
            # interprets the number as a display (port = 5900+display), so
            # we walk that band and grab the first free port. Replaces
            # `vnc=:0,to=99` so we know the chosen display up front and can
            # print it for the user.
            self.vnc_display = self._pick_vnc_display()

        if self.machine == "minimal":
            cloud_image = await self._ensure_minimal_cloudimg()
            await build_seed_iso(
                Path(self.workdir.name) / "seed.img",
                Path("test/minimal/user-data"),
                Path("test/minimal/meta-data"),
            )
            await self._create_overlay(
                str(cloud_image),
                f"{self.workdir.name}/disk.img",
                size="20G",
            )
            self.drives = [
                self._virtio_drive(f"{self.workdir.name}/disk.img"),
                f"file={self.workdir.name}/seed.img,if=virtio,format=raw",
            ]
            # x86_64's q35 falls back to SeaBIOS off the OS disk; aarch64's
            # `virt` boots only via UEFI, so flash is required there.
            if not self.arch.bios_boot_supported:
                self.drives += await self._uefi_drives()
        else:
            # ZFS variants pick a packer image (zfs or zfs-lab),
            # overlay its OS disks, and attach extra empty qcow2s on top for
            # the per-variant disk-setup script to format. See AGENTS.md
            # "Test Environment Design".
            packer_image = self._spec.packer_image
            if packer_image is None:
                # Bare assertion would be elided under `python -O`; raise so the
                # config error surfaces regardless of optimisation level.
                raise RuntimeError(f"non-minimal variant {self.machine!r} must declare packer_image")
            if self._image_dir_override is not None:
                image_dir = str(self._image_dir_override)
            else:
                image_dir = f"{self.imagedir}/{self.ubuntu_name}/{packer_image}"
            os_disk_count = self._spec.os_disk_count

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
            await self._copy_efivars_from(image_dir)
            self.drives += await self._uefi_drives()

        # launch.py --kernel/--initrd/--append: bypass the firmware boot
        # chain entirely and qemu-direct-boot a user-supplied kernel.
        # Useful for trying a custom kernel without rebuilding the image.
        if self._direct_boot_override is not None:
            self._direct_boot = self._direct_boot_override

        # Attach pflash on variants that don't already have it (x86_64
        # minimal BIOS) when launch.py asked for it via --with-pflash or an
        # explicit --efi-code/--efi-vars. Idempotent: skip when an earlier
        # branch already attached pflash (every ZFS variant + aarch64
        # minimal); the override flowed through there already.
        want_pflash = self._with_pflash or self._efi_code is not None or self._efi_vars is not None
        if want_pflash and not any("if=pflash" in d for d in self.drives):
            self.drives += await self._uefi_drives()

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
        """Create a qcow2 overlay pointing at *src* with optional resize.

        The backing file's format matches the packer artifact format, which
        is platform-dependent (raw on Linux, qcow2 on Mac).
        """

        args = ["qemu-img", "create", "-f", "qcow2", "-b", src, "-F", self._packer_disk_format, dest]
        if size:
            args.append(size)
        await run_command(args)

    async def _ensure_minimal_cloudimg(self) -> Path:
        """Download (once) the Ubuntu minimal cloud image used by the `minimal` variant.

        Pulls through the lab Nexus raw proxy by default; `--upstream-mirrors`
        bypasses to cloud-images.ubuntu.com directly.
        """
        # Ubuntu publishes minimal-cloudimg arm64 only from noble onwards;
        # jammy is amd64-only. Fail loud rather than 404'ing on the curl.
        if self.arch.cloud_image_suffix == "arm64" and self.ubuntu_name == "jammy":
            raise RuntimeError(f"Ubuntu does not publish a minimal-cloudimg for {self.ubuntu_name}/arm64. " "Use --ubuntu noble (or later) on arm64 hosts, " "or run --machine minimal on x86_64.")
        name = f"ubuntu-{self.ubuntu_version}-minimal-cloudimg-{self.arch.cloud_image_suffix}.img"
        cache = self.imagedir / "cloud-images"
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / name
        if target.exists():
            return target

        base = "https://cloud-images.ubuntu.com" if self.upstream_mirrors else "https://nexus.lab.fahm.fr/repository/ubuntu-cloud-images"
        url = f"{base}/minimal/releases/{self.ubuntu_name}/release/{name}"
        tmp = target.with_suffix(target.suffix + ".tmp")
        print_line(f"Downloading {url}")
        await run_command(["curl", "-fL", "--retry", "3", "-o", str(tmp), url])
        tmp.rename(target)
        return target

    async def _copy_efivars_from(self, image_dir: str) -> None:
        """Copy EFI vars for UEFI boots from *image_dir* into the workdir."""

        await run_command(
            ["cp", f"{image_dir}/efivars.fd", f"{self.workdir.name}/efivars.fd"],
        )

    def _virtio_drive(self, path: str) -> str:
        """Return a virtio drive string with sensible cache/discard flags."""

        return f"file={path},if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap"

    async def _uefi_drives(self) -> list[str]:
        """UEFI pflash code+vars pair, honouring efi_code/efi_vars overrides.

        The CODE blob defaults to arch.uefi_code_path_for (Homebrew on macOS,
        ovmf/qemu-efi-aarch64 on Linux); --efi-code overrides for custom
        EDK2/OVMF builds. The VARS blob is one of:

        - --efi-vars override, if supplied;
        - else {workdir}/efivars.fd, if it's been copied in via
          _copy_efivars_from (ZFS variants -- the packer image ships a
          primed efivars template so the bootloader entries survive across
          runs);
        - else a fresh empty file sized to the code blob -- right for
          ad-hoc launches like minimal or launch.py --with-pflash where
          there's no prior state. qemu pflash requires CODE and VARS to be
          the same size, and EDK2 builds aren't uniform: aarch64 EDK2 ships
          at 64 MiB, x86_64 OVMF typically at 4 MiB.
        """
        code_path = self._efi_code if self._efi_code is not None else uefi_code_path_for(self.arch)
        if self._efi_vars is not None:
            vars_path = self._efi_vars
        else:
            packer_vars = Path(f"{self.workdir.name}/efivars.fd")
            if packer_vars.exists():
                vars_path = packer_vars
            else:
                vars_path = Path(f"{self.workdir.name}/uefi-vars.fd")
                await run_command(["truncate", "-s", str(code_path.stat().st_size), str(vars_path)])
        return [
            f"file={code_path},if=pflash,unit=0,format=raw,readonly=on",
            f"file={vars_path},if=pflash,unit=1,format=raw",
        ]

    def _augment_kernel_cmdline(self, cmdline: str) -> str:
        """Backfill arch-appropriate console= entries on a direct-boot cmdline.

        cmdline arrives composed by provision.sh as
        "root=zfs:<bootfs> <org.zfsbootmenu:commandline>" -- the ZBM
        property is the canonical place to set per-pool boot args, so we
        honour it verbatim. If it doesn't already wire up this arch's
        serial UART we backfill defaults so qemu's `-serial stdio`
        receives kernel printk for the boot log.

        Match by serial_console_token so a property that already configures
        the right console doesn't get a duplicate appended. Order matters:
        Linux makes the LAST `console=` the primary /dev/console. We want
        serial primary (so ZBM TUI / login prompts land on -serial stdio
        in --foreground mode) and tty0 just secondary so VNC also gets
        kernel printk. Append tty0 first, then the arch-specific serial
        console.
        """

        extras: list[str] = []
        if self.keep_vm and "console=tty0" not in cmdline:
            # virtio-gpu-pci is attached when keep_vm=True, giving fbcon
            # something to bind to. Skipped headless -- without a graphics
            # device tty0 has nothing to render onto.
            extras.append("console=tty0")
        if self.arch.serial_console_token not in cmdline:
            extras.append(self.arch.serial_console_default)
        if not extras:
            return cmdline
        return f"{cmdline} {' '.join(extras)}"

    def _boot_command(self) -> list[str]:
        """Assemble the qemu command line for the prepared disks.

        Arch- and OS-aware: ArchProfile supplies the qemu binary, machine
        type, keep-VM device set, and serial console fallback; this method
        only chooses accel based on platform.system(). Display hardware
        (virtio-gpu-pci + qemu-xhci) works identically on both arches.
        """
        accel = "hvf" if platform.system() == "Darwin" else "kvm"

        if self.keep_vm:
            # q35 has std VGA + PS/2 keyboard by default but USB is opt-in
            # (machine flag usb=on, applied below); usb-tablet then attaches
            # to the built-in EHCI/UHCI for absolute-coordinate VNC mouse.
            # aarch64 virt has no default graphics or input devices, so it
            # needs the full virtio-gpu + xhci + usb-kbd set; both come from
            # ArchProfile.keep_vm_extra_devices.
            display_args = [
                "-display",
                # Display number pre-picked in prepare(); qemu binds to
                # 5900+display so the user can connect at 127.0.0.1:<port>.
                f"vnc=:{self.vnc_display}",
                *self.arch.keep_vm_extra_devices,
                "-k",
                "fr",
            ]
        else:
            display_args = ["-display", "none"]

        direct_boot: list[str] = []
        if self._direct_boot is not None:
            kernel, initrd, cmdline = self._direct_boot
            cmdline = self._augment_kernel_cmdline(cmdline)
            direct_boot = ["-kernel", str(kernel), "-initrd", str(initrd), "-append", cmdline]

        cmd = [
            "timeout",
            "--kill-after=10s",
            str(self.wrapper_timeout),
            self.arch.qemu_binary,
            *[arg for drive in self.drives for arg in ("--drive", drive)],
            *direct_boot,
            "-netdev",
            # Host port pre-picked in prepare() so we don't need to ask qemu
            # which port it ended up on -- avoids the prior lsof-poll dance
            # and the VNC-port-band collision risk.
            f"user,id=user.0,hostfwd=tcp:{SSH_HOST}:{self.ssh_port}-:22",
            "-object",
            "rng-random,id=rng0,filename=/dev/urandom",
            "-device",
            "virtio-rng-pci,rng=rng0",
            "-machine",
            # usb=on enables q35's built-in EHCI/UHCI controllers; needed
            # for usb-tablet under --keep-vm. Default-off has no cost when
            # usb-tablet isn't attached. virt machine ignores the flag and
            # uses qemu-xhci added above instead.
            f"type={self.arch.machine_type},accel={accel},usb=on",
            "-smp",
            f"{self._spec.vcpus},sockets=1,cores={self._spec.vcpus}",
            "-name",
            f"homelab-{self.machine}-{self.role}",
            "-m",
            f"{self._spec.memory_mb}M",
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

        # launch.py-only post-process. Each branch is a no-op when the
        # corresponding kwarg is at its default, so testrole.py / production
        # callers see the cmdline above verbatim.
        if self._mem is not None:
            cmd[cmd.index("-m") + 1] = self._mem

        if self._foreground:
            # Strip the `timeout --kill-after=10s 0 ...` wrapper. GNU timeout,
            # when not invoked directly from a shell prompt, detaches the
            # child from the controlling tty (it has a `--foreground` flag
            # specifically to opt out of that). Without that flag qemu can't
            # put the terminal into raw mode, so mon:stdio is unusable. We
            # don't need the wrapper in interactive mode anyway -- the user
            # quits via Ctrl-A,x.
            if cmd[0] != "timeout":
                # Raise so a future change to the wrapper layout surfaces
                # cleanly instead of silently slicing the wrong prefix.
                raise RuntimeError(f"expected timeout wrapper, got {cmd[:4]}")
            cmd = cmd[3:]
            # mon:stdio multiplexes the guest's first serial port with qemu's
            # HMP. Press Ctrl-A,c at the terminal to switch to HMP, Ctrl-A,c
            # again to return; Ctrl-A,x to quit qemu.
            cmd[cmd.index("-serial") + 1] = "mon:stdio"

        if self._qmp_socket is not None:
            cmd += ["-qmp", f"unix:{self._qmp_socket},server,nowait"]
        for path, tag in self._virtfs:
            cmd += [
                "-virtfs",
                f"local,id={tag},path={path},mount_tag={tag},security_model=mapped-xattr",
            ]
        return cmd

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
                # SIGINT mid-cleanup would leave qemu running. terminate_pid
                # SIGTERMs, polls for up to grace_seconds, then SIGKILLs.
                await asyncio.shield(terminate_pid(pid, grace_seconds=5))
        finally:
            await super().stop()

    async def _find_ssh_port(self) -> None:
        """Port was pre-picked in prepare(); nothing to discover at boot."""
        return
