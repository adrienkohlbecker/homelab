import asyncio
import contextlib
import errno
import fcntl
import functools
import ipaddress
import json
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, NamedTuple, Self

import yaml
from arch import ArchProfile, detect_host_arch, uefi_firmware_paths_for
from matrix import UBUNTU_RELEASES
from setup_mitogen import ensure_mitogen_symlink
from utils import (
    CommandResult,
    cancel_on_signal,
    print_cmd_line,
    print_line,
    read_and_write_stream,
    run_command,
    sleep_tick,
    terminate_pid,
    terminate_subprocess,
)

OUT_DIR = Path("test/out")

SSH_KEY = "packer/vagrant.key"
# Default loopback endpoint for qemu hostfwd binds, VNC displays, SSH, and
# delegated WAN probes. Each harness-driven cell overrides this with a private
# 127.x.y.z address (see _cell_loopback_host); launch.py pins it back here.
SSH_HOST = "127.0.0.1"
PIDFILE_NAME = "pid"


def _cell_loopback_host() -> str:
    """Per-process loopback bind address for this cell's qemu hostfwds.

    qemu's user-net (SLIRP) opens its own hostfwd listening sockets, so the
    harness can't pre-bind a socket and hand it to qemu -- it can only reserve a
    port, close it, and rely on qemu rebinding it before anything else grabs it
    (prepare()'s _reserve dance). Under a burst of co-tenant cells on one CI
    host that close->rebind window loses: two cells' `bind(addr, 0)` (or a host
    outbound connection) draw the same port from the shared ephemeral range and
    qemu dies on launch with "Could not set up host forwarding rule".

    Giving each cell its own 127.x.y.z address removes the contention by
    construction: bind uniqueness is per (addr, port), nothing else on the host
    sources traffic from this address, and sibling cells live on different
    addresses -- so a reused or freshly-released port can't collide. The whole
    127.0.0.0/8 is loopback on Linux, bindable with no setup.

    Keyed on PID: testall.py spawns one testrole.py subprocess per cell, so the
    PID is unique among the cells live on a host. Only 127.0.0.1 is configured
    on macOS by default (the rest of 127/8 needs `ifconfig lo0 alias`), and the
    Mac path isn't the bursty one, so non-Linux keeps the single loopback.
    """
    if platform.system() != "Linux":
        return SSH_HOST
    # PID fits the low 24 bits of 127.0.0.0/8 (default pid_max is 2^22, and the
    # kernel max is 2^22 on 32-bit / configurable to 2^30 on 64-bit -- masking
    # keeps us in-range, and live PIDs stay distinct in practice). pid >= 1, so
    # the network address 127.0.0.0 never occurs.
    pid = os.getpid() & 0xFFFFFF
    return f"127.{(pid >> 16) & 0xFF}.{(pid >> 8) & 0xFF}.{pid & 0xFF}"


TOPOLOGY_PATH = Path(__file__).parent.parent / "data" / "network_topology.yml"
WAN_PROBE_PORTS_PATH = Path(__file__).parent.parent / "data" / "wan_probe_ports.yml"


def _load_wan_probe_ports() -> dict[str, tuple[int, ...]]:
    """Load the shared controller-side WAN probe surface.

    QEMU maps these guest ports to random localhost ports.
    """
    data = yaml.safe_load(WAN_PROBE_PORTS_PATH.read_text()) or {}
    return {proto: tuple(int(port) for port in data.get(proto, ())) for proto in ("tcp", "udp")}


DEFAULT_WAN_FORWARDS = _load_wan_probe_ports()

# Absolute path to the repo's ansible.cfg. Pinned via ANSIBLE_CONFIG (see
# ansible_env) so it loads even when ansible would otherwise skip auto-discovery
# -- the GitLab CI checkout (/builds/akohlbecker/homelab) is world-writable, and
# ansible silently ignores an ansible.cfg in a world-writable cwd. Dropping it
# loses host_key_checking=False, the mitogen strategy, the vault ids, and the
# UserKnownHostsFile=/dev/null ssh_args, so the first connect to a fresh cell
# dies on "Host key verification failed". An explicit ANSIBLE_CONFIG bypasses
# the world-writable skip entirely.
ANSIBLE_CONFIG_PATH = Path(__file__).parent.parent / "ansible.cfg"


def _load_test_topology() -> dict:
    """Load data/network_topology.yml with the 10.123 → 10.234 gsub
    applied. The test harness always uses the test view regardless of
    which machine is selected — `test/inventory.ini` puts every
    machine (minimal/lab/pug) in the [test] group, so ansible
    consistently resolves `network.*` through group_vars/test.yml's
    gsub'd view. Mirror that here so the qemu user-net subnet matches.
    """
    text = TOPOLOGY_PATH.read_text().replace("10.123", "10.234")
    return yaml.safe_load(text)


def qemu_user_net_args(machine: str) -> str:
    """Comma-prefixed extras for `-netdev user,...` that pin the VM's
    primary NIC to its topology IP via slirp's `dhcpstart=`.

    Returns "" for machines absent from the topology (minimal), leaving QEMU
    on its default 10.0.2.0/24 user-net. Concurrent QEMU processes each run
    their own slirp, so identical net/dhcpstart across cells is fine -- slirps
    do not share state.
    """
    topo = _load_test_topology()
    host = topo["hosts"].get(machine)
    if not host:
        return ""
    physical = host["physical"]
    supernet = topo["partitions"]["physical"]["cidr"]
    net = ipaddress.ip_network(supernet)
    # Router + DNS at the top of the supernet, well above every host
    # slot (.0.2-.0.9) and every per-VLAN macvlan block (.X.128-.255),
    # so the qemu router never collides with a topology-claimed address.
    host_ip = str(net.broadcast_address - 1)
    dns_ip = str(net.broadcast_address - 2)
    return f",net={supernet},host={host_ip},dns={dns_ip},dhcpstart={physical}"


@functools.cache
def _passt_available(qemu_binary: str) -> bool:
    """True iff passt can back qemu *here*: Linux + `passt` on PATH + a qemu
    that advertises the `stream` netdev (i.e. qemu >= 7.2).

    Deliberately a capability probe, not a uname check: Linux installations
    may expose different qemu and passt versions, while macOS cannot use passt
    at all. Cached because the qemu probe forks a subprocess and the answer is
    constant per process.
    """
    if platform.system() != "Linux":
        return False
    if shutil.which("passt") is None:
        return False
    try:
        probe = subprocess.run(
            [qemu_binary, "-netdev", "help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except OSError, subprocess.SubprocessError:
        return False
    # qemu lists netdev types on stdout (older builds: stderr); check both.
    return "stream" in (probe.stdout + probe.stderr)


def resolve_net_backend(qemu_binary: str) -> str:
    """Pick the guest NIC backend: 'passt' or 'slirp'.

    passt is a userspace connector with a robust UDP datapath; it replaces
    qemu's libslirp on the guest-facing hop, killing the SLIRP-under-load UDP
    drops that flake external-DNS _verify in CI (see
    notes/archive/ci_qemu_net_passt_migration.md). It's only usable where
    `_passt_available` holds, so everywhere else (macOS or an image without
    the passt package) falls back to the unchanged slirp path.

    HOMELAB_NET_BACKEND overrides the probe: `slirp` pins the legacy path,
    `passt` forces it (and errors loudly if unavailable, so a misconfigured
    CI env fails fast instead of silently degrading), `auto` (default) probes.
    """
    override = os.environ.get("HOMELAB_NET_BACKEND", "auto").strip().lower()
    if override == "slirp":
        return "slirp"
    available = _passt_available(qemu_binary)
    if override == "passt":
        if not available:
            raise RuntimeError(
                "HOMELAB_NET_BACKEND=passt but passt is unusable here: it needs "
                "the `passt` binary on PATH and a qemu with the `stream` netdev "
                "(qemu >= 7.2). Install passt or unset the override."
            )
        return "passt"
    if override != "auto":
        raise RuntimeError(f"HOMELAB_NET_BACKEND={override!r} not in auto/slirp/passt")
    return "passt" if available else "slirp"


def passt_address_fields(machine: str) -> dict[str, str] | None:
    """The address/netmask/gateway that pin the guest to its topology IP, or
    None for machines absent from the topology (minimal).

    Mirrors `qemu_user_net_args`' slirp dhcpstart/host pinning so roles that
    key on the host's physical address see the same value under either backend.
    The gateway is the supernet's broadcast-1, exactly the `host=` slirp uses.
    Rendered as passt --address/--netmask/--gateway flags.
    """
    topo = _load_test_topology()
    host = topo["hosts"].get(machine)
    if not host:
        return None
    net = ipaddress.ip_network(topo["partitions"]["physical"]["cidr"])
    return {
        "address": host["physical"],
        "netmask": str(net.prefixlen),
        "gateway": str(net.broadcast_address - 1),
    }


class QemuMachineSpec(NamedTuple):
    ssh_user: str
    inventory_host: str
    cloud_image: bool = False
    # Guest RAM in MiB and vcpu count, plumbed into qemu's -m / -smp.
    # 4 vCPUs keeps 6 concurrent VMs at 24 logical cores (1.2x oversub
    # on the i5-13500's 20 threads) — converge is I/O-bound so this
    # doesn't bottleneck. -smp emits a single-socket layout
    # (sockets=1,cores=vcpus), the conventional shape for a guest on a
    # non-NUMA hypervisor.
    memory_mb: int = 4096
    vcpus: int = 4


QEMU_MACHINE_SPECS: dict[str, QemuMachineSpec] = {
    "minimal": QemuMachineSpec(
        ssh_user="ubuntu",
        inventory_host="minimal",
        cloud_image=True,
        memory_mb=2048,
        vcpus=2,
    ),
    "lab": QemuMachineSpec(
        ssh_user="vagrant",
        inventory_host="lab",
        # lab: matches the lab prod host. mdadm-EFI + mdadm-swap +
        # 3-disk mirror rpool + dozer + tank + mouse, all baked in.
        # Default integration fixture and promoted CI image.
    ),
    "pug": QemuMachineSpec(
        ssh_user="vagrant",
        inventory_host="pug",
        # Pug-specific single-disk rpool + apoc mirror fixture.
    ),
}


MACHINE_CHOICES: tuple[str, ...] = tuple(QEMU_MACHINE_SPECS)
_PACKER_DISK_RE = re.compile(r"packer-ubuntu-(\d+)\.(raw|qcow2)")


def discover_packer_disks(image_dir: Path) -> tuple[list[Path], str]:
    """Return contiguous Packer disks and their common on-disk format."""

    indexed: list[tuple[int, Path, str]] = []
    for path in image_dir.glob("packer-ubuntu-*"):
        match = _PACKER_DISK_RE.fullmatch(path.name)
        if match is None or not path.is_file():
            raise RuntimeError(f"unexpected Packer disk artifact: {path}")
        indexed.append((int(match.group(1)), path, match.group(2)))

    if not indexed:
        raise RuntimeError(f"no Packer disks found in {image_dir}")

    indexed.sort()
    indexes = [index for index, _, _ in indexed]
    expected = list(range(1, len(indexed) + 1))
    if indexes != expected:
        raise RuntimeError(f"Packer disk indexes in {image_dir} are {indexes}; expected {expected}")

    formats = {disk_format for _, _, disk_format in indexed}
    if len(formats) != 1:
        raise RuntimeError(f"Packer disks in {image_dir} mix formats: {sorted(formats)}")
    return [path for _, path, _ in indexed], formats.pop()


@dataclass(frozen=True)
class LaunchOptions:
    """QEMU boot options used by launch.py."""

    image_dir: Path | None = None
    kernel: Path | None = None
    initrd: Path | None = None
    append: str = ""
    mem: str | None = None
    with_pflash: bool = False
    virtfs: tuple[tuple[Path, str], ...] = ()
    foreground: bool = False
    display_window: bool = False
    headless: bool = False
    qmp_socket: Path | None = None
    extra_hostfwds: tuple[int, ...] = ()


@dataclass(frozen=True)
class MachineRunOptions:
    """Per-run policy that is independent of the role or artifact name."""

    vcpus: int | None = None
    memory_mb: int | None = None
    quiet_ansible: bool = False


SSH_WAIT_TIMEOUT = 120
IDFILE_TIMEOUT = 60
# Bounded shared-acquire window on the publish-lock. A wedged packer
# publish (holding LOCK_EX) would otherwise stall every concurrent test
# cell past its own --timeout; surface it as a clear TimeoutError with
# a debugging hint instead.
PUBLISH_LOCK_TIMEOUT = 300
# Bounded exclusive-acquire window on the per-image cloud-image download lock.
# The holder keeps it across the curl, so a waiter must outlast a full download
# of a few-hundred-MB image off a slow mirror; bounded so a wedged downloader
# surfaces rather than hanging every concurrent minimal cell.
CLOUDIMG_LOCK_TIMEOUT = 600


class Machine:
    """Start disposable QEMU guests for role-level integration tests."""

    # Extra seconds added on top of machine_timeout for the GNU `timeout` /
    # `podman --timeout` last-resort wrapper. Has to outlast the inner
    # asyncio.timeout in run_test so testrole.py's own deadline fires first
    # (and we get a clean rc=124 + stop()), with enough headroom for
    # Machine.stop() to do its graceful->SIGKILL escalation.
    WRAPPER_GRACE_SECONDS: ClassVar[int] = 60

    output_file: Path
    journal_file: Path
    boot_file: Path
    dmesg_file: Path
    systemctl_failed_file: Path
    passt_file: Path
    condition_coverage_file: Path
    workdir: tempfile.TemporaryDirectory[str]
    workdir_path: Path
    # fd of <workdir>/.live, held with fcntl.LOCK_EX|LOCK_NB for the lifetime
    # of the Machine. Liveness signal consumed by sweep_stale_workdirs(): the
    # kernel releases the lock on process death (clean or SIGKILL/OOM), so a
    # crashed run's workdir becomes reapable without a polling daemon. Survives
    # PID namespaces -- the lock is on the inode, shared across containers
    # that bind-mount the same workdir parent.
    _live_lock_fd: int
    # Shared flock on <imagedir>/.publish-lock held across prepare→
    # ensure_booted so packer-build's brief exclusive lock around its
    # install post-processor's atomic-rename (packer/publish.py) can't
    # tear our backing-file reads. Applies on both Linux (lab) and macOS
    # (a local `mise run packer:build` can race parallel testall.py cells
    # reading the same artifacts/ tree). Released at the end of
    # ensure_booted() once qemu's -drive open(2) has completed -- not at
    # the end of boot(), because create_subprocess_exec returns once the
    # kernel has fork+exec'd qemu but qemu doesn't open backing files
    # until after BIOS init (the qcow2-overlay backing path is embedded
    # by value in the overlay header, so a packer swap of that inode
    # between exec and open would silently corrupt the boot). -1 = not
    # held (also the steady state when the lockfile is absent -- a fresh
    # imagedir with no packer history).
    _publish_lock_fd: int
    # Controller-side WAN probe endpoint, so `delegate_to: localhost`
    # probes in roles/firewall's _verify can exercise rules keying on the
    # WAN interface (traffic originating inside the VM never ingresses on
    # the WAN iface). qemu slirp/passt forwards pre-picked free ports on the
    # cell's loopback (self.ssh_host), mapped to guest ports in wan_forward_ports.
    wan_forward_ports: dict[str, dict[str, int]]

    drives: list[str]
    # VNC display number (0..99) chosen in prepare() when keep_vm is True
    # and local GUI display is not requested;
    # consumed by _boot_command for the `-display vnc=` argument. Bound on
    # 5900+display so qemu won't try to walk the band itself.
    vnc_display: int
    # Extra guest ports to forward in addition to the configured probe ports.
    # Set by LaunchOptions.extra_hostfwds; populated in
    # prepare() as {guest_port: host_port}.
    extra_hostfwd_ports: dict[int, int]
    # Guest NIC backend, resolved once in __init__ (resolve_net_backend):
    # "passt" where the sidecar is usable, "slirp" everywhere else. The
    # sidecar is launched in boot() and torn down in stop(); these stay
    # None/unset on the slirp path.
    _net_backend: str
    _passt_socket: Path | None
    # Private dir holding _passt_socket, on the system tmpfs rather than the
    # /mnt/scratch workdir -- see the _passt_socket assignment for why. None on
    # the slirp path; torn down in _stop_passt.
    _passt_socket_dir: tempfile.TemporaryDirectory[str] | None
    _passt_proc: asyncio.subprocess.Process | None

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
        launch: LaunchOptions | None = None,
        run_options: MachineRunOptions | None = None,
        loopback_host: str | None = None,
    ):
        """QEMU-backed machine wrapper used by integration tests.

        launch carries launch.py-only qemu overrides. run_options carries
        explicit per-run resource and logging policy.

        loopback_host pins the 127.x bind address for this cell's hostfwds/SSH;
        None derives a per-process address (see _cell_loopback_host) so parallel
        cells don't collide. launch.py passes 127.0.0.1 to keep its
        --write-hostfwds contract (consumers assume the default loopback).
        """
        self.launch = launch or LaunchOptions()
        self.run_options = run_options or MachineRunOptions()
        try:
            spec = QEMU_MACHINE_SPECS[machine]
        except KeyError:
            raise AttributeError(f"Unknown machine: {machine}") from None

        spec = spec._replace(
            vcpus=(self.run_options.vcpus if self.run_options.vcpus is not None else spec.vcpus),
            memory_mb=(self.run_options.memory_mb if self.run_options.memory_mb is not None else spec.memory_mb),
        )

        self.imagedir: Path = imagedir_for_host()

        self._spec = spec
        if self.launch.image_dir is not None and spec.cloud_image:
            raise ValueError(f"image_dir override requires an artifact-backed variant, got {machine!r}")
        if (self.launch.kernel is None) != (self.launch.initrd is None):
            raise ValueError("launch kernel and initrd must be provided together")
        self.extra_hostfwd_ports: dict[int, int] = {}
        # Captured once at construction so prepare()/_boot_command() don't
        # have to re-run platform.machine() on every access.
        self.arch: ArchProfile = detect_host_arch()

        self.ssh_port = 0
        self.ssh_host = loopback_host if loopback_host is not None else _cell_loopback_host()
        self.ssh_user = spec.ssh_user
        self.inventory_host = spec.inventory_host
        self.machine = machine
        self.role = role
        self.keep_vm = keep_vm
        self.ubuntu_name = ubuntu_name
        self.machine_timeout = machine_timeout
        self.upstream_mirrors = upstream_mirrors
        self.proc: asyncio.subprocess.Process | None = None
        # Backgrounded `ssh -M -N` master, opened in ensure_ssh() once the
        # banner is up and torn down in stop(). Keeps a single ControlMaster
        # socket hot so every ansible-playbook phase (mirrors/_setup/check/
        # apply/idempotence/_verify) reuses it instead of paying a fresh
        # handshake + agent round-trip + mitogen bootstrap each.
        self._ssh_master_proc: asyncio.subprocess.Process | None = None
        self._live_lock_fd = -1
        self._publish_lock_fd = -1
        self._ansible_staged = False
        self._last_ansible_cmd: tuple[str, ...] | None = None
        self.wan_forward_ports = {"tcp": {}, "udp": {}}

        if self.ubuntu_name not in UBUNTU_RELEASES:
            raise ValueError(f"Unknown Ubuntu release '{self.ubuntu_name}'; known: {sorted(UBUNTU_RELEASES)}")
        prefix = f"{self.machine}.{self.ubuntu_name}.{self.role}"
        output_dir = OUT_DIR
        output_dir.mkdir(parents=True, exist_ok=True)
        self.output_file = output_dir / f"{prefix}.output.ansi"
        self.journal_file = output_dir / f"{prefix}.journal.ansi"
        self.boot_file = output_dir / f"{prefix}.boot.ansi"
        self.dmesg_file = output_dir / f"{prefix}.dmesg.ansi"
        self.systemctl_failed_file = output_dir / f"{prefix}.systemctl-failed.ansi"
        self.passt_file = output_dir / f"{prefix}.passt.ansi"
        coverage_name = f"{self.machine}.{self.ubuntu_name}.{self.arch.name}.{self.role}.jsonl"
        self.condition_coverage_file = output_dir / "condition_coverage" / coverage_name
        self._artifact_files = (
            self.output_file,
            self.journal_file,
            self.boot_file,
            self.dmesg_file,
            self.systemctl_failed_file,
            self.passt_file,
        )
        for stale in self._artifact_files:
            stale.unlink(missing_ok=True)
        self.condition_coverage_file.unlink(missing_ok=True)
        # The workdir lands alongside the packer qcow2s by default. An explicit
        # workdir_parent (CI flag) overrides so the imagedir can be ro-mounted.
        # Auto-create the parent so --workdir-parent /some/new/path just works
        # without callers having to mkdir -p first; tempfile itself doesn't
        # create the dir argument, only the per-run subdir under it.
        wd_parent = workdir_parent or self.imagedir
        Path(wd_parent).mkdir(parents=True, exist_ok=True)
        self.workdir = tempfile.TemporaryDirectory(dir=wd_parent)
        self.workdir_path = Path(self.workdir.name)
        # Claim the liveness lock immediately after the workdir exists so a
        # sweep racing us from another container can't reap the dir between
        # mkdtemp and the first qemu/ansible spawn. LOCK_NB so a contended
        # lock fails loudly (would only happen if two Machines somehow shared
        # a workdir, which mkdtemp prevents -- a BlockingIOError here is a
        # bug, not a race).
        self._live_lock_fd = os.open(self.workdir_path / ".live", os.O_WRONLY | os.O_CREAT, 0o644)
        fcntl.flock(self._live_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._preflight()

        # Resolve the NIC backend after _preflight has confirmed the qemu
        # binary exists, since the probe execs it.
        self._net_backend = resolve_net_backend(self.arch.qemu_binary)
        self._passt_socket = None
        self._passt_socket_dir = None
        self._passt_proc = None

    def _preflight(self) -> None:
        """Normalize the SSH key mode and verify required binaries.

        Called once at the end of __init__, after self.workdir exists, so the
        failure surface (binary checks, image cache lookups, etc.) is bounded
        to "things the harness will need before the next subprocess spawn".
        Failures raise RuntimeError with installation guidance.
        """
        ssh_key_path = Path(SSH_KEY)
        if ssh_key_path.exists():
            ssh_key_path.chmod(0o600)

        self._require_binary(
            self.arch.qemu_binary,
            f"Install via `brew install qemu` (macOS) or `apt install qemu-system-{self.arch.name}` (Debian/Ubuntu).",
        )
        # The boot wrapper uses GNU timeout; macOS doesn't ship one out of
        # the box, but `brew install coreutils` puts a `timeout` shim on PATH.
        self._require_binary(
            "timeout",
            "Install via `brew install coreutils` (macOS) or via the coreutils package on Linux.",
        )

    @staticmethod
    def _require_binary(name: str, hint: str) -> None:
        if shutil.which(name) is None:
            raise RuntimeError(f"Required binary {name!r} not found on PATH. {hint}")

    @property
    def pid_file(self) -> Path:
        """QEMU pidfile path under the per-run workdir."""
        return self.workdir_path / PIDFILE_NAME

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

    @property
    def ssh_control_path(self) -> str:
        """Stable ControlMaster socket path shared by every connection to this cell.

        One socket per cell, reused by the harness's own ssh AND by every
        ansible-playbook phase, so phases 2..N skip the SSH handshake + agent
        round-trip + mitogen interpreter bootstrap. Keyed on the cell's unique
        (ssh_host, ssh_port) -- the port alone can repeat across cells now that
        each binds a private loopback address, so the host is part of the key.
        Lives in /tmp (writable, short) rather than the per-cell workdir: workdir
        lands on /mnt/scratch on Linux CI hosts, and a unix socket path must stay
        under ~104 chars -- this /tmp path is short and per-cell unique.
        """
        return f"/tmp/homelab-cm-{self.ssh_host}-{self.ssh_port}"

    def _ssh_options(self) -> list[str]:
        """Return the shared `-o flag=value` pairs for harness SSH commands."""
        return [
            "-o",
            f"ControlPath={self.ssh_control_path}",
            # auto: reuse the master if it's up (the harness pre-opens it in
            # ensure_ssh), create one otherwise. ControlPersist keeps it warm
            # between the harness's intermittent ssh calls and the ansible
            # phases. Matches ansible.cfg's [ssh_connection] ssh_args so both
            # sides land on the same socket.
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPersist=600s",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "ConnectTimeout=10",
            # Surface a dead peer in ~60s. A cell that vanishes mid-task (spot
            # reclaim, network partition) leaves a half-open TCP the kernel
            # would otherwise hold for many minutes; without this, a command
            # reading from it hangs until the harness deadline instead of failing fast.
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=4",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "BatchMode=yes",
        ]

    def format_ssh_cmd(self, *cmd: str) -> list[str]:
        """Return an ssh invocation pinned to this instance."""

        # ForwardAgent=yes on every harness SSH connection means whichever
        # connection creates the ControlMaster seeds it with an agent-forwarding
        # channel. Otherwise ansible's later ForwardAgent=yes can silently reuse
        # an agent-less master and break roles that ssh to git@github.com from
        # the target.
        base = [
            "ssh",
            "-i",
            SSH_KEY,
            "-p",
            str(self.ssh_port),
            *self._ssh_options(),
            "-o",
            "ForwardAgent=yes",
            f"{self.ssh_user}@{self.ssh_host}",
        ]
        return [*base, shlex.join(cmd)] if cmd else base

    def ansible_env(self) -> dict[str, str]:
        """ANSIBLE_* environment overrides layered on top of os.environ.

        Fact cache lives inside the per-run workdir, so the ~9
        ansible-playbook invocations in one test share gathered facts
        (saves ~0.9s per replay) without leaking facts across runs that
        target a freshly-spawned host (different IP, different cgroup,
        different filesystem).
        """
        env = {
            "ANSIBLE_CONFIG": str(ANSIBLE_CONFIG_PATH),
            # Override [ssh_connection] ssh_args wholesale so ansible pins its
            # ControlPath to the cell-stable socket (ssh_control_path) instead
            # of its default per-invocation path. Without an explicit
            # ControlPath, each of the ~6 ansible-playbook processes opens its
            # own master; sharing one keeps the socket hot across phases. The
            # rest of the flags mirror ansible.cfg verbatim (ControlMaster,
            # ControlPersist, UserKnownHostsFile, ForwardAgent) so this doesn't
            # regress any of them -- the harness pre-opens the master in
            # ensure_ssh() and shares it via the same path.
            "ANSIBLE_SSH_ARGS": (
                f"-o ControlMaster=auto -o ControlPersist=600s -o ControlPath={self.ssh_control_path} "
                "-o UserKnownHostsFile=/dev/null -o ForwardAgent=yes"
            ),
            "ANSIBLE_DISPLAY_OK_HOSTS": "true",
            "ANSIBLE_DISPLAY_SKIPPED_HOSTS": "true",
            "ANSIBLE_GATHERING": "smart",
            # AWS cells can briefly starve sshd right after apt or service
            # restarts; keep the per-connection attempt bounded but less brittle
            # than Ansible's 10s default. The harness-level timeout still caps
            # the whole test.
            "ANSIBLE_TIMEOUT": "30",
            "ANSIBLE_FACT_CACHING": "jsonfile",
            "ANSIBLE_FACT_CACHING_CONNECTION": str(self.workdir_path / "facts"),
            "ANSIBLE_FACT_CACHING_TIMEOUT": "7200",
        }

        # The full-site converge runs ~4400 tasks; at the per-role default
        # (-v plus ok/skipped hosts shown) its transcript is ~90k lines and
        # blows past GitLab's 4 MB job-log cap, which truncates the tail --
        # exactly where the failing task lands. A converge smoke test only
        # needs the changed tasks (their diffs still print, [diff] always=True)
        # and full failure dumps (ansible prints a failed task's result
        # regardless of verbosity), so drop the ok/skipped firehose and the
        # -v result bodies for non-failing tasks here. Per-role tests keep the
        # verbose detail for single-role debugging.
        if self.run_options.quiet_ansible:
            env["ANSIBLE_DISPLAY_OK_HOSTS"] = "false"
            env["ANSIBLE_DISPLAY_SKIPPED_HOSTS"] = "false"
            env["ANSIBLE_VERBOSITY"] = "0"

        return env

    @property
    def in_aws(self) -> bool:
        """Whether this cell's guest egresses through AWS.

        Cloud-environment choices key on this -- the in-region EC2 apt/ECR
        mirrors are reachable while the LAN Nexus and AdGuard VIP are not.
        Driven by HOMELAB_TEST_IN_AWS: set by the aws_qemu CI cell (a qemu
        guest on an AWS shell runner), unset for local/lab qemu.
        """
        return os.environ.get("HOMELAB_TEST_IN_AWS", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )

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
            f"ansible_ssh_host={self.ssh_host}",
            "-e",
            f"ansible_ssh_user={self.ssh_user}",
            "-e",
            f"ansible_ssh_private_key_file={SSH_KEY}",
            # Static playbooks declare `hosts: all`; --limit pins the play to
            # the inventory host we actually provisioned.
            "--limit",
            self.inventory_host,
            # The static role dispatcher consumes the internal input directly;
            # group_vars/test.yml exposes the public fixture variable at normal
            # inventory precedence so task-scoped checks can vary it.
            "-e",
            f"_test_role_under_test={self.role}",
            # Internal harness inputs map to public vars in group_vars/test.yml.
            # Keeping them indirect lets task-scoped fixtures exercise alternate
            # environments without losing to extra-vars precedence.
            "-e",
            json.dumps(
                {
                    "_test_in_aws": self.in_aws,
                    "_test_nexus_url": "" if self.upstream_mirrors or self.in_aws else "nexus.lab.fahm.fr",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            # Controller-side WAN probe endpoint for verify probes that
            # delegate_to: localhost (see the wan_* field comment).
            "-e",
            f"wan_probe_host={self.ssh_host}",
            "-e",
            json.dumps(
                {"wan_forward_ports": self.wan_forward_ports},
                sort_keys=True,
                separators=(",", ":"),
            ),
            # Keepalives on the ansible/mitogen SSH transport too, matching
            # _ssh_options(): a cell that vanishes mid-task fails in ~60s
            # rather than hanging a mitogen read on a half-open TCP. JSON -e
            # form because the value has spaces (key=value would word-split).
            "-e",
            json.dumps(
                {"ansible_ssh_common_args": "-o ServerAliveInterval=15 -o ServerAliveCountMax=4"},
                separators=(",", ":"),
            ),
            "--inventory",
            "test/inventory.ini",
        ]
        if cmd:
            parts += cmd
        return parts

    async def ssh_command(self, *cmd: str, check: bool = True) -> CommandResult:
        """Execute an SSH command and stream output into the role log."""

        return await run_command(self.format_ssh_cmd(*cmd), check=check)

    async def ansible_command(
        self,
        *cmd: str,
        check: bool = True,
        coverage_phase: str | None = None,
    ) -> CommandResult:
        """Execute ansible-playbook with machine-specific SSH overrides."""

        self._stage_ansible_controller()
        self._last_ansible_cmd = cmd
        env = self.ansible_env()
        if coverage_phase is not None:
            callbacks = {
                name.strip() for name in os.environ.get("ANSIBLE_CALLBACKS_ENABLED", "").split(",") if name.strip()
            }
            callbacks.add("condition_coverage")
            env.update(
                {
                    "ANSIBLE_CALLBACKS_ENABLED": ",".join(sorted(callbacks)),
                    "ANSIBLE_CONDITION_COVERAGE_FILE": str(self.condition_coverage_file),
                    "ANSIBLE_CONDITION_COVERAGE_PHASE": coverage_phase,
                }
            )
        return await run_command(self.format_ansible_cmd(*cmd), check=check, env=env)

    def _stage_ansible_controller(self) -> None:
        """Populate controller inputs on demand before the first Ansible run."""

        if self._ansible_staged:
            return

        ensure_mitogen_symlink()

        for required_tree in ("group_vars", "host_vars", "roles", "data"):
            Path(required_tree).copy_into(self.workdir_path)

        # Role-local filter plugins are copied with roles/. A top-level
        # filter_plugins/, if present, also needs to sit beside the playbooks.
        # wireguard/ is gitignored (vaulted keys, never committed), so it is
        # optional; roles that need it fail later with the real missing input.
        for optional_tree in ("filter_plugins", "wireguard"):
            src = Path(optional_tree)
            if src.exists():
                src.copy_into(self.workdir_path)

        # These root files are valid playbook_dir-relative role inputs.
        for repo_root_file in ("mise.toml", "pyproject.toml", "uv.lock"):
            src = Path(repo_root_file)
            if src.exists():
                src.copy_into(self.workdir_path)

        # site_test.py may stage the production site.yml before its first
        # Ansible call; preserve that caller-owned override.
        for playbook in Path("test/playbooks").glob("*.yml"):
            destination = self.workdir_path / playbook.name
            if not destination.exists():
                playbook.copy_into(self.workdir_path)
        self._ansible_staged = True

    async def boot(self) -> None:
        """Bring up the passt sidecar (if any), then launch qemu under a timeout wrapper."""

        await self._start_passt()

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
        id_path = self.pid_file
        while not id_path.exists():
            if self.proc and self.proc.returncode is not None:
                raise RuntimeError(
                    f"Launching machine failed (qemu wrapper exited with {self.proc.returncode}); see {self.boot_file}"
                )
            if time.monotonic() > deadline:
                raise TimeoutError(f"PID file {id_path} not created within {IDFILE_TIMEOUT}s")
            await sleep_tick()

        # Drop the publish-lock now that qemu has opened its -drive
        # backing files. The PID/CID file is written by qemu after
        # device init (which includes the qcow2-overlay open(2) and
        # therefore the backing-file open(2)), so by the time we get
        # here the kernel holds open fds on every inode our overlays
        # point at -- a packer rename of those paths from this point
        # on is invisible to us. Holding the lock longer would block
        # packer's publish (which is rare but bounded; our acquire is
        # shared, packer's is exclusive).
        self._release_publish_lock()

    async def ensure_ssh(self) -> None:
        """Wait for the daemon banner on the port reserved in prepare()."""

        deadline = time.monotonic() + SSH_WAIT_TIMEOUT
        while not await self._ssh_banner_ready():
            if time.monotonic() > deadline:
                raise TimeoutError("SSH daemon did not become ready in time")
            await sleep_tick()

        await self._open_ssh_master()

    async def ensure_system_running(self) -> None:
        """Require systemd to finish booting in a healthy running state."""
        result = await self.ssh_command("systemctl", "is-system-running", "--wait", check=False)
        state = "\n".join(result.stdout).strip()
        if result.exitcode == 0 and state == "running":
            print_line(f"System fully booted: {state}")
            return

        failed = await self.ssh_command("systemctl", "--failed", "--no-legend", check=False)
        failed_units = "\n".join(failed.stdout).rstrip() or "(none)"
        raise RuntimeError(f"System reached state {state!r} (rc={result.exitcode}); failed units:\n{failed_units}")

    async def _open_ssh_master(self) -> None:
        """Background a persistent `ssh -M -N` master on the cell-stable ControlPath.

        Opened the moment the banner is ready so the socket is hot before the
        mirrors phase, sparing every later ansible/harness connection a fresh
        handshake + agent round-trip + mitogen bootstrap. `-N` (no command) +
        `-M` (master) just establishes the multiplexing socket and parks; it
        carries ForwardAgent so the master seeds an agent channel for roles
        that ssh out to git@github.com (see format_ssh_cmd's block comment).
        Best-effort: if it can't come up, ControlMaster=auto on the individual
        connections still creates a master on first use -- we don't gate the
        run on it. Torn down in stop() via `ssh -O exit`.
        """
        cmd = [
            "ssh",
            "-M",
            "-N",
            "-f",
            "-i",
            SSH_KEY,
            "-p",
            str(self.ssh_port),
            *self._ssh_options(),
            "-o",
            "ForwardAgent=yes",
            f"{self.ssh_user}@{self.ssh_host}",
        ]
        print_cmd_line(cmd)
        # -f backgrounds ssh itself once the master socket is up, so the
        # subprocess exits promptly and we don't hold a Process handle. The
        # parked master lives on past it, reaped by `ssh -O exit` in stop().
        self._ssh_master_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(15):
                await self._ssh_master_proc.wait()

    async def ensure_cloud_init(self) -> None:
        """Block until cloud-init's config and final stages finish.

        SSH opens during cloud-init's network stage. Waiting here prevents the
        first converge from racing its package locks and /etc/hosts rewrite.
        A degraded-but-complete run may return non-zero, so the result is not a
        gate.
        """
        await self.ssh_command("sudo", "cloud-init", "status", "--wait", check=False)

    async def _ssh_banner_ready(self) -> bool:
        """Probe the SSH port once. Return True iff a non-empty banner arrives."""

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.ssh_host, self.ssh_port),
                timeout=2,
            )
        except OSError, TimeoutError:
            # sshd not yet accepting connections; caller will retry.
            return False

        try:
            banner_bytes = await asyncio.wait_for(reader.read(1024), timeout=2)
            return bool(banner_bytes.decode().strip())
        except OSError, TimeoutError:
            # Connected but no banner in time; treat as not-ready.
            return False
        finally:
            writer.close()
            # wait_closed() returns immediately for a healthy transport, but a
            # half-open connection through qemu's SLIRP hostfwd (port accepted
            # while sshd is still coming up at first boot) can stall the close
            # handshake indefinitely. Left unbounded it hangs the whole probe,
            # so the SSH_WAIT_TIMEOUT deadline never gets re-checked and a flaky
            # boot burns the full per-test timeout instead of failing in ~2min.
            # This is best-effort cleanup -- the transport is reaped with the VM
            # regardless -- so cap it and move on. OSError covers a peer that
            # dropped the connection before our close completed.
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), timeout=2)

    async def _collect_remote_to_file(self, label: str, dest: Path, *remote_cmd: str) -> None:
        """Run *remote_cmd* over SSH, capture stdout into *dest*.

        stderr is streamed to the main log; stdout goes only to *dest*.
        *label* is what we print when the capture fails or succeeds. Used by
        the per-run failure diagnostics so each artifact (journal, dmesg,
        systemctl --failed) is a separate file the operator (or CI artifact
        upload) can read in isolation.
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
        else:
            print_line(f"{label}: {dest}")

    async def collect_failure_artifacts(self) -> None:
        """Collect post-mortem diagnostics from the guest after a failed run.

        Three artifacts:
          - <variant>.<role>.journal.ansi -- full systemd journal
          - <variant>.<role>.dmesg.ansi -- guest kernel ring buffer
          - <variant>.<role>.systemctl-failed.ansi -- list of failed units

        Each runs as a best-effort capture so a failure of one doesn't shadow
        the others. The files remain available for local inspection and CI
        artifact collection.
        """

        captures = (
            (
                "Systemd journal",
                self.journal_file,
                (
                    "env",
                    "SYSTEMD_COLORS=true",
                    "journalctl",
                    "--no-pager",
                    "--priority",
                    "info",
                ),
            ),
            (
                "Kernel ring buffer",
                self.dmesg_file,
                ("sudo", "dmesg", "--color=always", "--ctime"),
            ),
            (
                "Failed units",
                self.systemctl_failed_file,
                ("env", "SYSTEMD_COLORS=true", "systemctl", "--failed", "--no-pager"),
            ),
        )
        for label, dest, remote_cmd in captures:
            try:
                await self._collect_remote_to_file(label, dest, *remote_cmd)
            except Exception as exc:
                # Diagnostics are best-effort and must not mask the original
                # test failure or prevent the remaining captures.
                print_line(f"Failed to collect {label}: {exc}")

    def cleanup_logs(self) -> None:
        """Remove all per-run log artifacts."""
        for path in self._artifact_files:
            path.unlink(missing_ok=True)

    async def wait(self) -> None:
        if self.proc:
            await self.proc.wait()

    @contextlib.asynccontextmanager
    async def session(self, timeout: int) -> AsyncIterator[None]:
        """Run under the harness timeout, signal, and keep-VM policy."""
        task = asyncio.current_task()
        assert task is not None
        timer_absorbed = False

        with cancel_on_signal(task):
            async with asyncio.timeout(timeout) as timeout_cm:
                async with self:
                    try:
                        try:
                            yield
                        except asyncio.CancelledError:
                            if self.keep_vm and timeout_cm.expired() and task.cancelling():
                                task.uncancel()
                                timer_absorbed = True
                                print_line(f"Timed out after {timeout}s; --keep set, dropping to SSH for debug")
                            else:
                                raise
                    finally:
                        if self.keep_vm and not task.cancelling():
                            with contextlib.suppress(RuntimeError):
                                # A fired deadline cannot be rescheduled, but it is
                                # already spent and no longer bounds the debug wait.
                                timeout_cm.reschedule(None)
                            self.print_ssh_instructions()
                            await self.wait()

        if timer_absorbed:
            raise TimeoutError()

    async def __aenter__(self) -> Self:
        await self.prepare()
        await self.boot()
        return self

    async def __aexit__(self, exc_type: object, exc: BaseException | None, tb: object) -> None:
        print_line("Stopping machine...")
        await self.stop()

    async def stop(self) -> None:
        """Kill qemu via its pidfile, then drain the timeout wrapper and free temp resources.

        Signaling self.proc (the `timeout` wrapper) normally forwards SIGINT
        to qemu, but if the wrapper is SIGKILL'd or testrole.py dies before
        stop() runs, qemu reparents to init with no recovery path -- SIGKILL
        can't be caught and forwarded. Kill qemu directly via its pidfile so
        cleanup works regardless of the wrapper's fate.

        With qemu and the passt sidecar dead, the wrapper (`timeout`) should
        notice and exit on its own immediately -- we just wait for it. SIGKILL
        after 5s in case something pathological keeps it alive (zombie
        subprocess, hung pipe).
        """
        pid_path = self.pid_file
        pid: int | None = None
        if pid_path.exists():
            with contextlib.suppress(ValueError):
                pid = int(pid_path.read_text().strip())

        try:
            if pid is not None:
                # Shield against nested cancellation; without it a second
                # SIGINT mid-cleanup would leave qemu running. terminate_pid
                # SIGTERMs, polls for up to grace_seconds, then SIGKILLs.
                await asyncio.shield(terminate_pid(pid, grace_seconds=5))
        finally:
            await self._close_ssh_master()
            await self._stop_passt()
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
                # Defensive release: boot() drops the publish-lock on its happy
                # path, but if it raised between acquire and release we'd leak
                # the fd into the process beyond. Re-call is idempotent (no-op
                # when fd<0).
                self._release_publish_lock()
                # Release the liveness lock before rmtree -- the kernel would
                # release it on close()/exit anyway, but doing it explicitly
                # keeps the ordering obvious.
                if self._live_lock_fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(self._live_lock_fd)
                    self._live_lock_fd = -1
                self.workdir.cleanup()

    def _release_publish_lock(self) -> None:
        """Drop the shared publish-lock fd if held; no-op otherwise.

        Idempotent so callers (ensure_booted's happy path + stop's finally) can both
        invoke it without coordinating. The kernel would release the flock
        on close() anyway; explicit close keeps the ordering legible.
        """
        if self._publish_lock_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self._publish_lock_fd)
            self._publish_lock_fd = -1

    def print_ssh_instructions(self) -> None:
        ssh_cmd = shlex.join(self.format_ssh_cmd())
        print_line("Keeping VM around, ssh using:")
        print_line(f"> {ssh_cmd}")
        if self._last_ansible_cmd is not None:
            resume_cmd = [
                "env",
                *(f"{key}={value}" for key, value in self.ansible_env().items()),
                *self.format_ansible_cmd(*self._last_ansible_cmd),
            ]
            print_line("Replay the last Ansible phase against this fixture from the repository root:")
            print_line(f"> {shlex.join(resume_cmd)}")
            print_line("Add --start-at-task 'TASK NAME' or --step to resume within that phase.")
            print_line("This uses staged code; rerun testrole.py after editing repository files.")
        print_line("Then Ctrl+C to stop the machine")
        if self.launch.display_window:
            print_line("Display: QEMU window")
        else:
            # vnc_display is only set when keep_vm=True and local GUI display
            # is disabled; print_ssh_instructions itself is also keep-only.
            print_line(f"VNC: {self.ssh_host}:{5900 + self.vnc_display}")

    def _pick_vnc_display(self) -> int:
        """Walk VNC ports 5900..5999 and return the first free display number.

        qemu's `vnc=:N` syntax binds to port 5900+N, so we test bind on the
        actual port and hand qemu the matching display. Mirrors qemu's own
        `to=99` walk but resolves up front so we know the chosen display
        before launch (and can print it). Raises if all 100 are occupied.
        """
        for display in range(100):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind((self.ssh_host, 5900 + display))
                    return display
            except OSError:
                continue
        raise RuntimeError(f"No free VNC display in 0..99 on {self.ssh_host}")

    async def prepare(self) -> None:
        """Create overlay images and seed data for the selected template."""

        # Acquire the publish-lock before any read of the imagedir starts.
        # _create_overlay's qemu-img embeds the backing file's absolute path
        # by value, so a packer install post-processor that rm+mv's the
        # parent dir between overlay-create and qemu-launch would silently
        # corrupt the boot. The shared lock composes with packer's exclusive
        # lock on the same path -- packer waits for all in-flight test
        # launches to drop, then publishes, then we re-acquire. Released at
        # the end of boot() once qemu has pinned the inodes via open fds.
        self._acquire_publish_lock_shared()

        # Reserve every hostfwd port up front by binding an ephemeral socket on
        # self.ssh_host and reading back the assigned port. Avoids an lsof-poll
        # heuristic, which would have to filter VNC ports and re-tangle if any
        # future qemu service published TCP. Every reservation socket stays
        # open until all ports are picked: closing one before the next bind
        # lets the kernel re-hand-out the just-released port, and two forwards
        # sharing a host port make qemu refuse to launch outright ("Could not
        # set up host forwarding rule"). Uniqueness only has to hold within a
        # protocol -- qemu keys hostfwd on proto+hostport -- so the TCP and UDP
        # reservations are independent. There is still a tiny race between
        # closing the sockets and qemu's bind, but each cell binds a private
        # loopback address (_cell_loopback_host), so the only contender for a
        # just-released (addr, port) is this same sequential process -- no
        # co-tenant cell or host outbound connection sources from this address.
        self.wan_forward_ports = {"tcp": {}, "udp": {}}
        reserved: list[socket.socket] = []

        def _reserve(sock_type: int) -> int:
            s = socket.socket(socket.AF_INET, sock_type)
            s.bind((self.ssh_host, 0))
            reserved.append(s)
            return s.getsockname()[1]

        try:
            # SSH endpoint -- loopback hostfwd, pinned the same way as the rest.
            self.ssh_port = _reserve(socket.SOCK_STREAM)
            # Auxiliary forwards for controller-side probes that need to
            # ingress on the VM's WAN iface; emitted into qemu/passt
            # unconditionally so the qemu cmdline doesn't have to know which
            # role's running.
            for proto, guest_ports in DEFAULT_WAN_FORWARDS.items():
                sock_type = socket.SOCK_STREAM if proto == "tcp" else socket.SOCK_DGRAM
                for guest_port in guest_ports:
                    key = str(guest_port)
                    if key in self.wan_forward_ports[proto]:
                        continue
                    self.wan_forward_ports[proto][key] = _reserve(sock_type)
            for guest_port in self.launch.extra_hostfwds:
                key = str(guest_port)
                if key in self.wan_forward_ports["tcp"]:
                    self.extra_hostfwd_ports[guest_port] = self.wan_forward_ports["tcp"][key]
                    continue
                self.extra_hostfwd_ports[guest_port] = _reserve(socket.SOCK_STREAM)
                self.wan_forward_ports["tcp"][key] = self.extra_hostfwd_ports[guest_port]
        finally:
            for s in reserved:
                s.close()

        # On the passt backend qemu connects to the sidecar over a unix
        # socket. It must NOT live in self.workdir: that's on /mnt/scratch
        # (the ZFS qemu-image volume, sized for the multi-GB disks), where
        # passt's listening socket immediately epoll-errors and the sidecar
        # exits ("Error on listening Unix socket, exiting"). Give it a private
        # dir on the system tmpfs instead -- the same constraint packer/
        # qemu_net_wrapper.py meets via tempfile.mkdtemp(). qemu reaches it
        # since both run in this container. Launched in boot() so its lifetime
        # brackets qemu's; torn down in _stop_passt. None on the slirp path.
        if self._net_backend == "passt":
            self._passt_socket_dir = tempfile.TemporaryDirectory(prefix="homelab-passt-", ignore_cleanup_errors=True)
            self._passt_socket = Path(self._passt_socket_dir.name) / "passt.sock"

        if self.keep_vm and not self.launch.display_window and not self.launch.headless:
            # qemu's vnc= syntax interprets the number as a display
            # (port = 5900+display); pick it up front so we can print it.
            self.vnc_display = self._pick_vnc_display()

        if self._spec.cloud_image:
            cloud_image = await self._ensure_minimal_cloudimg()
            seed_img = self.workdir_path / "seed.img"
            disk_img = self.workdir_path / "disk.img"
            await run_command(
                [
                    "xorrisofs",
                    "-output",
                    str(seed_img),
                    "-volid",
                    "cidata",
                    "-joliet",
                    "-rock",
                    "test/minimal/user-data",
                    "test/minimal/meta-data",
                ]
            )
            await self._create_overlay(
                str(cloud_image),
                str(disk_img),
                size="20G",
                backing_fmt="qcow2",
            )
            self.drives = [
                self._virtio_drive(str(disk_img)),
                f"file={seed_img},if=virtio,format=raw",
            ]
            # x86_64 q35 can fall back to SeaBIOS; aarch64 virt requires UEFI.
            if not self.arch.bios_boot_supported:
                self.drives += await self._uefi_drives()
        else:
            # Artifact-backed variants overlay every disk Packer published.
            if self.launch.image_dir is not None:
                image_dir = self.launch.image_dir.resolve()
            else:
                image_dir = self.imagedir / self.ubuntu_name / self.machine
            os_src_paths, artifact_format = discover_packer_disks(image_dir)

            os_disk_paths: list[str] = []
            for idx, src in enumerate(os_src_paths, start=1):
                dest = self.workdir_path / f"packer-ubuntu-{idx}"
                await self._create_overlay(str(src), str(dest), backing_fmt=artifact_format)
                os_disk_paths.append(str(dest))

            self.drives = [self._virtio_drive(path, "qcow2") for path in os_disk_paths]
            shutil.copyfile(image_dir / "efivars.fd", self.workdir_path / "efivars.fd")
            self.drives += await self._uefi_drives()

        # Attach pflash when launch.py requested it and the selected path did
        # not already require it (for example x86_64 minimal under SeaBIOS).
        if self.launch.with_pflash and not any("if=pflash" in d for d in self.drives):
            self.drives += await self._uefi_drives()

    async def _create_overlay(self, src: str, dest: str, *, backing_fmt: str, size: str | None = None) -> None:
        """Create a qcow2 overlay pointing at *src* with optional resize.

        lazy_refcounts defers refcount-table updates so cluster writes don't
        block on metadata flushes — safe because overlays are ephemeral (a
        refcount leak on crash just means a slightly larger file, which we
        delete anyway).
        """

        args = [
            "qemu-img",
            "create",
            "-f",
            "qcow2",
            "-o",
            "lazy_refcounts=on",
            "-b",
            src,
            "-F",
            backing_fmt,
            dest,
        ]
        if size:
            args.append(size)
        await run_command(args)

    def _acquire_publish_lock_shared(self) -> None:
        """Hold a shared flock on <imagedir>/.publish-lock until ensure_booted returns.

        Applies on macOS too (parallel `test/testall.py` cells can race
        against a local `mise run packer:build` rebuild on the same
        artifacts/ tree). Skipped only when the lockfile is absent --
        packer's install post-processor touches it before flocking, so
        any imagedir that has had at least one packer-build will have
        the file. On a fresh imagedir with no packer history we fall
        through to the unlocked path rather than failing the boot.

        LOCK_NB+deadline rather than blocking LOCK_SH: a wedged packer
        publish (holding LOCK_EX) would otherwise block every concurrent
        test cell indefinitely, past the harness's own --timeout --
        surface it as a clear error with a debugging hint instead.
        """
        lockfile = self.imagedir / ".publish-lock"
        if not lockfile.exists():
            return
        fd = os.open(str(lockfile), os.O_RDONLY)
        end = time.monotonic() + PUBLISH_LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                self._publish_lock_fd = fd
                return
            except OSError as e:
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    os.close(fd)
                    raise
                if time.monotonic() >= end:
                    os.close(fd)
                    raise TimeoutError(
                        f"publish-lock held >{PUBLISH_LOCK_TIMEOUT:.0f}s; "
                        f"concurrent packer-build wedged? check `lsof {lockfile}`"
                    ) from e
                time.sleep(0.5)

    async def _ensure_minimal_cloudimg(self) -> Path:
        """Download and cache the Ubuntu minimal cloud image.

        Local runs use the Nexus proxy by default; AWS cells and
        --upstream-mirrors fetch directly from cloud-images.ubuntu.com.
        """
        name = f"ubuntu-{UBUNTU_RELEASES[self.ubuntu_name]}-minimal-cloudimg-{self.arch.cloud_image_suffix}.img"
        cache = self.imagedir / "cloud-images"
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / name
        if target.exists():
            return target

        # Several cells share this cache; wait without blocking the event loop.
        lockfile = cache / f"{name}.lock"
        fd = os.open(str(lockfile), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            end = time.monotonic() + CLOUDIMG_LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as e:
                    if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                        raise
                    if time.monotonic() >= end:
                        raise TimeoutError(
                            f"cloud-image lock held >{CLOUDIMG_LOCK_TIMEOUT:.0f}s; "
                            f"concurrent cell wedged? check `lsof {lockfile}`"
                        ) from e
                    await asyncio.sleep(0.5)
            # A peer may have completed the download while this process waited.
            if target.exists():
                return target
            base = (
                "https://cloud-images.ubuntu.com"
                if self.upstream_mirrors or self.in_aws
                else "https://nexus.lab.fahm.fr/repository/ubuntu-cloud-images"
            )
            url = f"{base}/minimal/releases/{self.ubuntu_name}/release/{name}"
            # A unique temporary path plus atomic replace prevents concurrent
            # cells from publishing a partial download.
            tmp = cache / f"{name}.{os.getpid()}.tmp"
            print_line(f"Downloading {url}")
            await run_command(["curl", "-fL", "--retry", "3", "-o", str(tmp), url])
            os.replace(tmp, target)
            return target
        finally:
            os.close(fd)

    def _virtio_drive(self, path: str, format: str = "qcow2") -> str:
        """Return a virtio drive string with sensible cache/discard flags."""

        aio = "io_uring" if platform.system() == "Linux" else "threads"
        return f"file={path},if=virtio,cache=unsafe,aio={aio},discard=unmap,format={format},detect-zeroes=unmap"

    async def _uefi_drives(self) -> list[str]:
        """Return the auto-detected UEFI code and writable vars pair.

        The CODE blob comes from arch.uefi_firmware_paths_for. The VARS blob is
        one of:

        - {workdir}/efivars.fd, copied from the packer image for ZFS
          variants so bootloader entries survive across runs;
        - else the architecture's pinned VARS template when it has one;
        - else a fresh empty file sized to the code blob.

        qemu pflash requires CODE and VARS to be the same size, and EDK2 builds
        aren't uniform: aarch64 EDK2 ships at 64 MiB, x86_64 OVMF typically at
        4 MiB.
        """
        code_path, vars_template = uefi_firmware_paths_for(self.arch)
        packer_vars = self.workdir_path / "efivars.fd"
        if packer_vars.exists():
            vars_path = packer_vars
        else:
            vars_path = self.workdir_path / "uefi-vars.fd"
            if vars_template is not None:
                shutil.copyfile(vars_template, vars_path)
            else:
                with vars_path.open("wb") as handle:
                    handle.truncate(code_path.stat().st_size)
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

    def _netdev_args(self) -> tuple[str, str]:
        """Return the (`-netdev` value, `-device` value) for the NIC backend.

        passt attaches qemu to the sidecar over a `stream` netdev; slirp uses
        qemu's legacy user-mode net with hostfwds. The forward set is the same
        either way -- SSH for ansible-playbook plus wan_forward_ports for the
        firewall `_verify` probes that `delegate_to: localhost`.
        """
        if self._net_backend == "passt":
            assert self._passt_socket is not None
            netdev = f"stream,id=net0,server=off,addr.type=unix,addr.path={self._passt_socket}"
            return netdev, f"{self.arch.net_device},netdev=net0"
        # Ports pre-picked in prepare(). qemu_user_net_args pins the VM's eth0
        # to network.hosts[inventory_host].physical (10.234.x test view); it is
        # empty for minimal, which has no topology identity.
        hostfwds = [f"hostfwd=tcp:{self.ssh_host}:{self.ssh_port}-:22"]
        for proto in ("tcp", "udp"):
            hostfwds.extend(
                f"hostfwd={proto}:{self.ssh_host}:{host_port}-:{guest_port}"
                for guest_port, host_port in self.wan_forward_ports[proto].items()
            )
        netdev = f"user,id=user.0,{','.join(hostfwds)}{qemu_user_net_args(self.inventory_host)}"
        return netdev, f"{self.arch.net_device},netdev=user.0"

    def _passt_command(self) -> list[str]:
        """Build the passt sidecar argv for this machine's forwarded ports."""
        tcp_forwards = [
            f"{self.ssh_port}:22",
            *(f"{host_port}:{guest_port}" for guest_port, host_port in self.wan_forward_ports["tcp"].items()),
        ]
        udp_forwards = [f"{host_port}:{guest_port}" for guest_port, host_port in self.wan_forward_ports["udp"].items()]
        # One address prefix binds the entire comma-list; repeating it makes
        # passt reject the value as an invalid port specifier.
        tcp_spec = f"{self.ssh_host}/{','.join(tcp_forwards)}"
        udp_spec = f"{self.ssh_host}/{','.join(udp_forwards)}" if udp_forwards else None
        assert self._passt_socket is not None
        cmd = [
            "passt",
            # Foreground: a managed child (torn down in stop()) that logs to
            # stderr instead of the syslog socket absent in the container.
            "--foreground",
            "--quiet",
            # Quit once qemu (the only client) disconnects so a leaked sidecar
            # can't outlive its VM; stop() also kills it explicitly as backup.
            "--one-off",
            "--socket",
            str(self._passt_socket),
            "--tcp-ports",
            tcp_spec,
        ]
        if udp_spec is not None:
            cmd += ["--udp-ports", udp_spec]
        fields = passt_address_fields(self.inventory_host)
        if fields is not None:
            cmd += [
                "--address",
                fields["address"],
                "--netmask",
                fields["netmask"],
                "--gateway",
                fields["gateway"],
            ]
        return cmd

    async def _start_passt(self) -> None:
        """Launch the passt sidecar and block until its socket is listening.

        No-op on the slirp backend. Runs before qemu (in boot()) so the socket
        exists when qemu's `stream` netdev connects. The sidecar logs to a
        per-run .passt.ansi beside the boot log for post-mortem. The socket
        wait is bounded so a wedged passt fails fast rather than hanging the
        run to its outer timeout.
        """
        if self._net_backend != "passt":
            return
        assert self._passt_socket is not None
        cmd = self._passt_command()
        print_cmd_line(cmd)
        with self.passt_file.open("wb") as handle:
            self._passt_proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=handle,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + 10
        while not self._passt_socket.exists():
            if self._passt_proc.returncode is not None:
                raise RuntimeError(f"passt exited before creating its socket; see {self.passt_file}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"passt socket {self._passt_socket} not created within 10s")
            await sleep_tick()

    def _boot_command(self) -> list[str]:
        """Assemble the qemu command line for the prepared disks.

        Arch- and OS-aware: ArchProfile supplies the qemu binary, machine
        type, keep-VM device set, and serial console fallback; this method
        only chooses accel based on platform.system(). Display hardware
        (virtio-gpu-pci + qemu-xhci) works identically on both arches.
        """
        accel = "hvf" if platform.system() == "Darwin" else "kvm"

        if self.launch.headless:
            display_args = ["-display", "none"]
        elif self.keep_vm:
            # q35 has std VGA + PS/2 keyboard by default but USB is opt-in
            # (machine flag usb=on, applied below); usb-tablet then attaches
            # to the built-in EHCI/UHCI for absolute-coordinate mouse.
            # aarch64 virt has no default graphics or input devices, so it
            # needs the full virtio-gpu + xhci + usb-kbd set; both come from
            # ArchProfile.keep_vm_extra_devices.
            display_backend = "cocoa" if platform.system() == "Darwin" else "gtk"
            display_args = [
                "-display",
                (
                    display_backend
                    if self.launch.display_window
                    # Display number pre-picked in prepare(); bind VNC to this
                    # cell's loopback (self.ssh_host) -- a bare `vnc=:N` binds the
                    # wildcard host, so the reservation in _pick_vnc_display (which
                    # probes self.ssh_host) and the actual bind would disagree, and
                    # two parallel --keep cells reserving the same display N on
                    # different loopbacks would still collide on the wildcard port.
                    else f"vnc={self.ssh_host}:{self.vnc_display}"
                ),
                *self.arch.keep_vm_extra_devices,
                "-k",
                "fr",
            ]
        else:
            display_args = ["-display", "none"]

        direct_boot: list[str] = []
        if self.launch.kernel is not None:
            assert self.launch.initrd is not None
            cmdline = self._augment_kernel_cmdline(self.launch.append)
            direct_boot = [
                "-kernel",
                str(self.launch.kernel.resolve()),
                "-initrd",
                str(self.launch.initrd.resolve()),
                "-append",
                cmdline,
            ]

        netdev_arg, net_device_arg = self._netdev_args()

        cmd = [
            "timeout",
            "--kill-after=10s",
            str(self.wrapper_timeout),
            self.arch.qemu_binary,
            *[arg for drive in self.drives for arg in ("--drive", drive)],
            *direct_boot,
            "-netdev",
            netdev_arg,
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
            net_device_arg,
            "-pidfile",
            str(self.pid_file),
        ]

        # launch.py-only post-process. Each branch is a no-op when the
        # corresponding kwarg is at its default, so testrole.py / production
        # callers see the cmdline above verbatim.
        if self.launch.mem is not None:
            cmd[cmd.index("-m") + 1] = self.launch.mem

        if self.launch.foreground:
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

        if self.launch.qmp_socket is not None:
            cmd += ["-qmp", f"unix:{self.launch.qmp_socket},server,nowait"]
        for path, tag in self.launch.virtfs:
            cmd += [
                "-virtfs",
                f"local,id={tag},path={path},mount_tag={tag},security_model=mapped-xattr",
            ]
        return cmd

    async def _stop_passt(self) -> None:
        """Tear down the passt sidecar and remove its socket dir.

        --one-off makes passt quit when qemu disconnects, so by the time we
        get here it has usually exited on its own; the terminate/kill is the
        backstop for the paths where qemu was SIGKILLed without a clean
        disconnect. The socket-dir cleanup runs regardless. No-op on slirp
        (both _passt_proc and _passt_socket_dir stay None there).
        """
        proc = self._passt_proc
        if proc is not None and proc.returncode is None:
            await terminate_subprocess(proc, grace_seconds=5, initial_signal=signal.SIGTERM)
        # Drop the socket's private tmpdir once passt is gone (it unlinks the
        # socket itself on exit; this clears the parent). Runs whether or not
        # passt had already self-exited via --one-off.
        if self._passt_socket_dir is not None:
            self._passt_socket_dir.cleanup()
            self._passt_socket_dir = None

    async def _close_ssh_master(self) -> None:
        """Tear down the persistent ssh ControlMaster so no socket leaks across runs.

        `ssh -O exit` signals the parked master to close cleanly and unlink its
        socket; without it the master would linger ControlPersist=600s past the
        cell, and a same-port future cell could reuse a stale socket pointing at
        a dead guest. Best-effort -- the master may already be gone (ssh -f's
        wrapper exited, ControlPersist expired). No-op when never opened.
        """
        if self._ssh_master_proc is None:
            return
        self._ssh_master_proc = None
        cmd = [
            "ssh",
            "-O",
            "exit",
            "-o",
            f"ControlPath={self.ssh_control_path}",
            "-p",
            str(self.ssh_port),
            f"{self.ssh_user}@{self.ssh_host}",
        ]
        with contextlib.suppress(OSError, TimeoutError):
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            async with asyncio.timeout(5):
                await proc.wait()


def imagedir_for_host() -> Path:
    """Return the platform's packer-image cache root.

    HOMELAB_CI_DIR is authoritative under mise. Direct invocations fall back
    to /mnt/scratch/homelab_ci on Linux or <repo>/packer/artifacts on Mac.
    Linux raises if the selected mountpoint is missing; Mac creates it.
    """
    system = platform.system()
    if system == "Darwin":
        d = Path(os.environ.get("HOMELAB_CI_DIR", "packer/artifacts")).resolve()
        d.mkdir(parents=True, exist_ok=True)
        return d
    if system == "Linux":
        d = Path(os.environ.get("HOMELAB_CI_DIR", "/mnt/scratch/homelab_ci")).resolve()
        if not d.is_dir():
            raise RuntimeError(f"Imagedir {d!s} does not exist; mount or create the configured qemu image volume.")
        return d
    raise RuntimeError(f"Unknown operating system: {system}")


def sweep_stale_workdirs(imagedir: Path) -> None:
    """Reap orphaned tmp* harness workdirs from prior runs.

    Each live Machine holds an exclusive flock on <workdir>/.live. The mtime
    grace guards the window between mkdtemp and lock acquisition; after that,
    an uncontended lock identifies a workdir left by a dead process.
    """
    if not imagedir.is_dir():
        return

    grace_seconds = 60
    now = time.time()

    for d in imagedir.iterdir():
        if not d.is_dir() or not d.name.startswith("tmp"):
            continue
        try:
            age = now - d.stat().st_mtime
        except OSError:
            continue
        if age < grace_seconds:
            continue

        if not _workdir_is_orphan(d):
            continue

        print_line(f"Reaping orphaned workdir {d}")
        try:
            shutil.rmtree(d)
        except OSError as exc:
            print_line(f"  (reap failed: {exc.strerror} — read-only mount?)")


def _workdir_is_orphan(workdir: Path) -> bool:
    """True iff the harness liveness lock on <workdir>/.live is unheld.

    Missing .live file means the workdir predates this mechanism (or was
    created by a non-harness tool) -- treat as orphan, the mtime grace in
    the caller is the only safety net. An open()/flock() failure other than
    "contended" also means orphan: the file is gone or unreadable, nothing
    live could be holding it.
    """
    live = workdir / ".live"
    try:
        fd = os.open(live, os.O_RDONLY)
    except FileNotFoundError:
        return True
    except OSError:
        return True
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)
