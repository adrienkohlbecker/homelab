#!/usr/bin/env -S uv run

import asyncio
import contextlib
import dataclasses
import errno
import fcntl
import functools
import ipaddress
import os
import platform
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import ClassVar, NamedTuple, Self

import yaml

from arch import ArchProfile, detect_host_arch, uefi_code_path_for
from setup_mitogen import ensure_mitogen_symlink
from utils import (
    CommandResult,
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

TOPOLOGY_PATH = Path(__file__).parent.parent / "data" / "network_topology.yml"


def _load_test_topology() -> dict:
    """Load data/network_topology.yml with the 10.123 → 10.234 gsub
    applied. The test harness always uses the test view regardless of
    which machine is selected — `test/inventory.ini` puts every
    machine (lab/pug/box/minimal) in the [test] group, so ansible
    consistently resolves `network.*` through group_vars/test.yml's
    gsub'd view. Mirror that here so the qemu user-net subnet matches.
    """
    text = TOPOLOGY_PATH.read_text().replace("10.123", "10.234")
    return yaml.safe_load(text)


def qemu_user_net_args(machine: str) -> str:
    """Comma-prefixed extras for `-netdev user,...` that pin the VM's
    primary NIC to its topology IP via slirp's `dhcpstart=`.

    Returns "" for machines absent from the topology (e.g. `minimal`),
    leaving qemu on its default 10.0.2.0/24 user-net. Concurrent qemu
    processes each run their own slirp, so identical net/dhcpstart
    across cells is fine — slirps don't share state.
    """
    topo = _load_test_topology()
    host = topo["hosts"].get(machine)
    if not host:
        return ""
    physical = host["physical"]
    supernet = topo["partitions"]["physical"]["cidr"]
    net = ipaddress.ip_network(supernet)
    # Router + DNS at the top of the supernet, well above every host
    # slot (.0.2–.0.9) and every per-VLAN macvlan block (.X.128–.255),
    # so the qemu router never collides with a topology-claimed address.
    host_ip = str(net.broadcast_address - 1)
    dns_ip = str(net.broadcast_address - 2)
    return f",net={supernet},host={host_ip},dns={dns_ip},dhcpstart={physical}"


@functools.cache
def _passt_available(qemu_binary: str) -> bool:
    """True iff passt can back qemu *here*: Linux + `passt` on PATH + a qemu
    that advertises the `stream` netdev (i.e. qemu >= 7.2).

    Deliberately a capability probe, not a uname check: the jammy host
    (qemu 6.2, no passt) and the noble ci-image (qemu 8.2 + passt) are *both*
    Linux, so uname can't tell them apart -- but the operator runs the harness
    directly on the jammy host occasionally and that path must keep using
    slirp. macOS short-circuits first (passt is Linux-only). Cached because
    the qemu probe forks a subprocess and the answer is constant per process.
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
    except (OSError, subprocess.SubprocessError):
        return False
    # qemu lists netdev types on stdout (older builds: stderr); check both.
    return "stream" in (probe.stdout + probe.stderr)


def resolve_net_backend(qemu_binary: str) -> str:
    """Pick the guest NIC backend: 'passt' or 'slirp'.

    passt is a userspace connector with a robust UDP datapath; it replaces
    qemu's libslirp on the guest-facing hop, killing the SLIRP-under-load UDP
    drops that flake external-DNS _verify in CI (see
    notes/ci_qemu_net_passt_migration.md). It's only usable where
    `_passt_available` holds, so everywhere else (jammy host, macOS, any image
    without the passt package) falls back to the unchanged slirp path.

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


def passt_address_args(machine: str) -> list[str]:
    """passt -a/-n/-g flags pinning the guest to its topology IP, mirroring
    `qemu_user_net_args`' slirp dhcpstart/host pinning so roles that key on the
    host's physical address see the same value under either backend.

    Empty for machines absent from the topology (e.g. `minimal`): passt then
    assigns from the container's default-route interface, matching slirp's
    default-net behaviour there. The gateway is the supernet's broadcast-1,
    exactly the `host=` slirp uses.
    """
    topo = _load_test_topology()
    host = topo["hosts"].get(machine)
    if not host:
        return []
    net = ipaddress.ip_network(topo["partitions"]["physical"]["cidr"])
    gateway = str(net.broadcast_address - 1)
    return ["--address", host["physical"], "--netmask", str(net.prefixlen), "--gateway", gateway]


# git only tracks the executable bit; a fresh checkout (notably CI's
# `actions/checkout@v4`) lands the vagrant key at 0644 and ssh refuses
# to use it ("UNPROTECTED PRIVATE KEY FILE"). chmod once at import
# time -- idempotent, invisible to git, and ensures every harness
# entrypoint is covered without sprinkling the fix at each call site.
_ssh_key_path = Path(SSH_KEY)
if _ssh_key_path.exists():
    _ssh_key_path.chmod(0o600)


class QemuMachineSpec(NamedTuple):
    ssh_user: str
    inventory_host: str
    # Packer image directory under /mnt/scratch/qemu/<ubuntu_name>/. None means the
    # variant uses an Ubuntu cloud image instead (minimal).
    packer_image: str | None
    # Number of disks the packer image stages as part of the OS install
    # (includes rpool + every extra pool disk). prepare() overlays
    # packer-ubuntu-1..N.{raw,qcow2} for those. Unused on minimal
    # (packer_image=None), where the cloud-image branch returns early.
    os_disk_count: int = 0
    # Guest RAM in MiB and vcpu count, plumbed into qemu's -m / -smp.
    # 4 vCPUs keeps 6 concurrent VMs at 24 logical cores (1.2× oversub
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
        packer_image=None,
        memory_mb=2048,
        vcpus=2,
    ),
    "box": QemuMachineSpec(
        ssh_user="vagrant",
        inventory_host="box",
        # box: single-disk rpool, no extra pools. Minimal ZFS-on-root
        # fixture used by push CI; producer-role coverage that needs
        # apoc/dozer/tank/mouse moves to lab/pug nightly.
        packer_image="box",
        os_disk_count=1,
    ),
    "box_deps": QemuMachineSpec(
        ssh_user="vagrant",
        inventory_host="box",
        # box_deps: same disks/inventory as box, but the packer build
        # pre-bakes podman (with the noble backports applied) and
        # nginx + snakeoil cert via packer/seed_deps.yml. Roles opt in
        # via roles/<role>/meta/test.yml's `machine: box_deps`. Reuses
        # host_vars/box.yml because inventory_host stays box.
        # 5 GiB: box_deps roles pull large container images and run them
        # during converge (HA alone is 2.4 GB on disk, ~1 GB RSS at
        # startup); the expanded nginx_site assert+validate chain runs
        # concurrently with the container startup, and 4 GiB is no
        # longer enough headroom.
        packer_image="box_deps",
        memory_mb=5120,
        os_disk_count=1,
    ),
    "lab": QemuMachineSpec(
        ssh_user="vagrant",
        inventory_host="lab",
        # lab: matches the lab prod host. mdadm-EFI + mdadm-swap +
        # 3-disk mirror rpool + dozer + tank + mouse, all baked in.
        # Push CI doesn't fan out to lab; kept for on-demand
        # --machine lab debug + nightly + packer script regression.
        packer_image="lab",
        os_disk_count=9,
    ),
    "pug": QemuMachineSpec(
        ssh_user="vagrant",
        inventory_host="pug",
        # pug: matches the pug prod host. Single-disk rpool + apoc
        # mirror, all baked in. Push CI doesn't fan out to pug;
        # kept for on-demand --machine pug + nightly + packer script
        # regression.
        packer_image="pug",
        os_disk_count=3,
    ),
}


MACHINE_CHOICES: tuple[str, ...] = tuple(QEMU_MACHINE_SPECS)


def resolve_default_machine(role: str) -> str:
    """Read roles/<role>/meta/test.yml and return its `machine:` field.

    Falls back to 'box' when the file is absent or doesn't declare
    machine. Exits non-zero on a parse error or an unknown machine
    name -- a typo'd opt-in should fail loudly at startup, not run
    silently against box.
    """
    meta_path = Path(f"roles/{role}/meta/test.yml")
    if not meta_path.exists():
        return "box"
    try:
        data = yaml.safe_load(meta_path.read_text()) or {}
    except yaml.YAMLError as e:
        sys.exit(f"{meta_path}: parse error: {e}")
    machine = data.get("machine", "box")
    if machine not in MACHINE_CHOICES:
        sys.exit(f"{meta_path}: machine={machine!r} not in {sorted(MACHINE_CHOICES)}")
    return machine


def _qemu_ansible_args(spec: QemuMachineSpec) -> list[str]:
    """Return any -e overlay needed on top of inventory-loaded host_vars.

    For test-only inventory hosts (box, minimal) the test fixture lives
    directly in host_vars/<host>.yml and ansible loads it naturally; no
    extra-vars layer is needed. For variants whose inventory host
    doubles as a prod host (lab, pug), host_vars/<host>-qemu.yml carries
    the VM-incompatible overrides (qemu_test, fake netplan, no UPS,
    test-mode macos_vm) on top of the prod-shaped host_vars/<host>.yml
    that inventory loads; we force-load it via -e so its values beat
    the prod host_vars regardless of merge order. qemu_test itself
    lives inside each fixture/overlay file, not harness-injected.
    """
    override = Path(f"host_vars/{spec.inventory_host}-qemu.yml")
    if not override.exists():
        return []
    return ["-e", f"@{override}"]


SSH_WAIT_TIMEOUT = 120
IDFILE_TIMEOUT = 60
# Bounded shared-acquire window on the publish-lock. A wedged packer
# publish (holding LOCK_EX) would otherwise stall every concurrent test
# cell past its own --timeout; surface it as a clear TimeoutError with
# a debugging hint instead.
PUBLISH_LOCK_TIMEOUT = 300

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
    # fd of <workdir>/.live, held with fcntl.LOCK_EX|LOCK_NB for the lifetime
    # of the Machine. Liveness signal consumed by sweep_stale_workdirs(): the
    # kernel releases the lock on process death (clean or SIGKILL/OOM), so a
    # crashed run's workdir becomes reapable without a polling daemon. Survives
    # PID namespaces -- the lock is on the inode, shared across containers
    # that bind-mount the same workdir parent. Replaces the prior ps-based
    # check, which was PID-ns-scoped and so couldn't see sibling Gitea Actions
    # test cells running in separate containers on the same host bind-mount.
    _live_lock_fd: int = dataclasses.field(default=-1, init=False)
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
    _publish_lock_fd: int = dataclasses.field(default=-1, init=False)
    peak_rss_kb: int = dataclasses.field(default=0, init=False)
    # Auxiliary slirp hostfwds. Pre-picked free 127.0.0.1 ports map
    # the controller side to specific VM ports so `delegate_to:
    # localhost` probes can exercise rules keying on the WAN interface
    # (traffic originating inside the VM never ingresses on enp0s2 via
    # slirp's user-mode net). Two protocols since slirp keeps TCP/UDP
    # forwards in separate namespaces.
    #   wan_tcp_test_port      → controller TCP → VM:32400 (iptables _verify
    #                        busybox-httpd publish target)
    #   wan_udp_test_port  → controller UDP → VM:51820 (iptables _verify
    #                        wireguard ACCEPT rule, scoped -i WAN)
    # 0 = unset (non-QEMU machine, or before prepare() picks one).
    wan_tcp_test_port: int = dataclasses.field(default=0, init=False)
    wan_udp_test_port: int = dataclasses.field(default=0, init=False)

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
        # Claim the liveness lock immediately after the workdir exists so a
        # sweep racing us from another container can't reap the dir between
        # mkdtemp and the first qemu/ansible spawn. LOCK_NB so a contended
        # lock fails loudly (would only happen if two Machines somehow shared
        # a workdir, which mkdtemp prevents -- a BlockingIOError here is a
        # bug, not a race).
        self._live_lock_fd = os.open(f"{self.workdir.name}/.live", os.O_WRONLY | os.O_CREAT, 0o644)
        fcntl.flock(self._live_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
        # (e.g. compta: the harness's pre-playbook scp can win the race
        # over the mirrors playbook's ssh and the master comes up
        # agent-less).
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
            # Auxiliary slirp hostfwd ports (controller-side) for verify
            # probes that delegate_to: localhost. 0 when not applicable
            # (non-QEMU machine, or before prepare() ran).
            "-e",
            f"wan_tcp_test_port={self.wan_tcp_test_port}",
            "-e",
            f"wan_udp_test_port={self.wan_udp_test_port}",
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

    async def prepare(self) -> None:
        """Stage a temporary workdir with inventory snippets and the static playbooks."""

        Path("group_vars").copy_into(self.workdir.name)
        Path("host_vars").copy_into(self.workdir.name)
        Path("roles").copy_into(self.workdir.name)
        # Role-local filter plugins (e.g. roles/wireguard/filter_plugins/)
        # are already staged via the roles/ copy above. A top-level
        # filter_plugins/ directory, if present, is also staged so playbook-
        # scoped filters resolve.
        if Path("filter_plugins").exists():
            Path("filter_plugins").copy_into(self.workdir.name)
        # group_vars/{prod,test}.yml derive `network.*` via
        # `lookup('file', playbook_dir ~ '/data/network_topology.yml')`;
        # the file has to be present alongside the staged playbooks for
        # the lookup to resolve.
        Path("data").copy_into(self.workdir.name)
        # wireguard/ is gitignored (vaulted keys, never committed) so it's
        # absent on a CI checkout. Skip silently when missing -- roles that
        # actually need its contents will fail later with a clearer error.
        if Path("wireguard").exists():
            Path("wireguard").copy_into(self.workdir.name)

        # mise.toml + uv lock + pyproject are repo-root files that some
        # roles reference via `{{ playbook_dir }}/<file>` (e.g. github_runner
        # bakes them into its ci-image container build context).
        # Stage them so the harness's workdir mirrors what ansible sees
        # on a production controller run.
        for repo_root_file in ("mise.toml", "pyproject.toml", "uv.lock"):
            src = Path(repo_root_file)
            if src.exists():
                src.copy_into(self.workdir.name)

        # github_runner's ci-image build COPYs packer/qemu.pkr.hcl
        # into its container build context (so the image bakes in the
        # packer plugins our packer-build workflow uses) and our input-
        # hash gating lookups it from playbook_dir. Stage just the .hcl
        # file -- copying the whole packer/ tree would also drag in
        # artifacts/ (multi-GB qcow2s) and scripts/ which aren't needed
        # by any role under test.
        packer_src = Path("packer/qemu.pkr.hcl")
        if packer_src.exists():
            packer_dest_dir = Path(self.workdir.name) / "packer"
            packer_dest_dir.mkdir(exist_ok=True)
            packer_src.copy_into(packer_dest_dir)

        # Copy the static role-agnostic playbooks (site / _setup /
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
        """Resolve SSH port then wait for the daemon banner to appear."""

        await self._find_ssh_port()

        deadline = time.monotonic() + SSH_WAIT_TIMEOUT
        while not await self._ssh_banner_ready():
            if time.monotonic() > deadline:
                raise TimeoutError("SSH daemon did not become ready in time")
            await sleep_tick()

    async def ensure_cloud_init(self) -> None:
        """Block until cloud-init's config/final stages finish before converge.

        ensure_ssh only waits for the sshd banner, which opens in cloud-init's
        network stage; its config stage (apt sources, manage_etc_hosts, package
        installs) is still running. A converge or apt call that starts before
        that settles races cloud-init's dpkg locks and its /etc/hosts rewrite --
        the same race packer's provision.sh closes with a cloud-init wait of its
        own. `cloud-init status --wait` blocks through the final stage; it exits
        non-zero on a degraded-but-complete run, so don't gate on the result.
        """
        await self.ssh_command("sudo", "cloud-init", "status", "--wait", check=False)

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

        Idempotent so callers (boot's happy path + stop's finally) can both
        invoke it without coordinating. The kernel would release the flock
        on close() anyway; explicit close keeps the ordering legible.
        """
        if self._publish_lock_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self._publish_lock_fd)
            self._publish_lock_fd = -1


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
            raise RuntimeError(
                f"Imagedir {str(d)!r} does not exist. "
                f"Mount the qemu image volume (e.g. `sudo mount /mnt/scratch/qemu`)."
            )
        return d
    raise RuntimeError(f"Unknown operating system: {system}")


def sweep_stale_workdirs(imagedir: Path) -> None:
    """Reap orphaned tmp* (harness) and .build-* (packer) dirs from prior runs.

    Cleanup normally rides Machine.__aexit__'s finally chain (machine.py:579)
    for tmp* and the trailing rmdir in mise-tasks/packer/build.sh for .build-*.
    Both bypass on SIGKILL / OOM / power-loss, leaving orphan dirs. Each is a
    full repo copy plus a qcow2 overlay -- ansible-lint also walks into them
    until .ansible-lint excludes the path, so leaks are doubly expensive.

    Liveness check is split by dir kind:

    * tmp* (harness workdirs) -- each live Machine holds an exclusive flock on
      <workdir>/.live. Sweep tries LOCK_EX|LOCK_NB on that file: contended =
      live (skip), uncontended = orphan (reap). flock is inode-scoped, so it
      works across PID namespaces -- critical because Gitea Actions test cells
      run in separate containers that bind-mount a shared workdir parent
      (/scratch), and the prior ps-based check couldn't see
      sibling cells' qemu/ansible. The mtime grace still guards the tiny
      window between mkdtemp and Machine.__post_init__'s flock acquisition.

    * .build-* (packer) -- packer doesn't (yet) hold a liveness lock, so these
      keep the ps-args check. Safe because packer-build runs alone under the
      `concurrency: lab-qemu-artifacts` workflow lock, so a single ps scan
      sees the only candidate process.
    """
    if not imagedir.is_dir():
        return

    grace_seconds = 60
    now = time.time()
    candidates = [
        d for d in imagedir.iterdir() if d.is_dir() and (d.name.startswith("tmp") or d.name.startswith(".build-"))
    ]
    if not candidates:
        return

    ps_args: str | None = None

    for d in candidates:
        try:
            age = now - d.stat().st_mtime
        except OSError:
            continue
        if age < grace_seconds:
            continue

        if d.name.startswith("tmp"):
            if not _workdir_is_orphan(d):
                continue
        else:
            # .build-* path: fall back to ps scan, lazily computed.
            if ps_args is None:
                try:
                    ps_args = subprocess.run(["ps", "-Ao", "args="], check=True, capture_output=True, text=True).stdout
                except (subprocess.CalledProcessError, FileNotFoundError):
                    # Don't reap when we can't verify staleness.
                    return
            if d.name in ps_args:
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
    # VNC display number (0..99) chosen in prepare() when keep_vm is True
    # and local GUI display is not requested;
    # consumed by _boot_command for the `-display vnc=` argument. Bound on
    # 5900+display so qemu won't try to walk the band itself.
    vnc_display: int
    # Set only when launch.py passes --kernel/--initrd/--append (ad-hoc custom
    # kernel without rebuilding the image). _boot_command() emits
    # -kernel/-initrd/-append and the firmware boot chain is bypassed.
    # None in normal harness operation on all arches and variants.
    _direct_boot: tuple[Path, Path, str] | None
    # Extra guest ports to forward in addition to the standard three (22,
    # 32400, 51820). Set by extra_hostfwds= in __init__; populated in
    # prepare() as {guest_port: host_port}.
    extra_hostfwd_ports: dict[int, int]
    # Guest NIC backend, resolved once in __init__ (resolve_net_backend):
    # "passt" inside the noble ci-image (robust UDP datapath), "slirp"
    # everywhere passt can't run. The passt sidecar (a separate process
    # qemu connects to over a unix socket) is launched in boot() and torn
    # down in stop(); these stay None/unset on the slirp path.
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
        display_window: bool = False,
        qmp_socket: Path | None = None,
        commit_in_place: bool = False,
        extra_hostfwds: list[int] | None = None,
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
        via Ctrl-A,c. display_window swaps the keep-VM display backend from
        VNC to a local qemu GUI window. qmp_socket binds qemu's QMP server to
        a unix socket. These last set are launch.py-only knobs; testrole.py /
        production callers leave them at defaults.
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
        self._direct_boot_override: tuple[Path, Path, str] | None = (
            (kernel.resolve(), initrd.resolve(), append) if kernel is not None else None
        )
        self._mem = mem
        self._with_pflash = with_pflash
        self._efi_code = efi_code.resolve() if efi_code is not None else None
        self._efi_vars = efi_vars.resolve() if efi_vars is not None else None
        self._virtfs: list[tuple[Path, str]] = list(virtfs or [])
        self._foreground = foreground
        self._display_window = display_window
        # commit_in_place: skip the qcow2-overlay step for the OS disks and
        # mount the image_dir's packer-ubuntu-N.<format> files as the qemu
        # drives directly. Writes during the run mutate those files in
        # place — that's the whole point, since mise-tasks/packer/seed-deps.sh
        # stages a fresh copy of box's artifacts into a tmpdir, runs
        # launch.py --commit --seed against it, and then publishes the
        # mutated tmpdir as box_deps. Refuses unless image_dir is also
        # set, so a stray `launch.py --commit` against the published
        # variant directory can't corrupt the source artifacts.
        if commit_in_place and image_dir is None:
            raise ValueError("commit_in_place=True requires image_dir to be set explicitly")
        self._commit_in_place = commit_in_place
        self._qmp_socket = qmp_socket
        self._extra_guest_ports: list[int] = list(extra_hostfwds or [])
        self.extra_hostfwd_ports: dict[int, int] = {}
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
        # Resolve the NIC backend after _preflight (run via super().__init__)
        # has confirmed the qemu binary exists, since the probe execs it.
        self._net_backend = resolve_net_backend(self.arch.qemu_binary)
        self._passt_socket = None
        self._passt_socket_dir = None
        self._passt_proc = None

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
        if self._display_window:
            print_line("Display: QEMU window")
        else:
            # vnc_display is only set when keep_vm=True and local GUI display
            # is disabled; print_ssh_instructions itself is also keep-only.
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
            "Install via `brew install qemu` (macOS) "
            f"or `apt install qemu-system-{self.arch.name}` (Debian/Ubuntu).",
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

        # Acquire the publish-lock before any read of the imagedir starts.
        # _create_overlay's qemu-img embeds the backing file's absolute path
        # by value, so a packer install post-processor that rm+mv's the
        # parent dir between overlay-create and qemu-launch would silently
        # corrupt the boot. The shared lock composes with packer's exclusive
        # lock on the same path -- packer waits for all in-flight test
        # launches to drop, then publishes, then we re-acquire. Released at
        # the end of boot() once qemu has pinned the inodes via open fds.
        self._acquire_publish_lock_shared()

        self._direct_boot = None

        # Pre-pick a free TCP port on 127.0.0.1 and pin qemu's hostfwd to it.
        # Avoids an lsof-poll heuristic, which would have to filter VNC ports
        # and re-tangle if any future qemu service published TCP. Tiny
        # race window between close() and qemu's bind, but qemu launches
        # near-immediately and the kernel rarely reissues a freshly-released
        # port within microseconds.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((SSH_HOST, 0))
            self.ssh_port = s.getsockname()[1]
        # Auxiliary hostfwd ports for controller-side probes that need
        # to ingress on the VM's WAN iface. Two protocols since slirp
        # keeps TCP/UDP forwards in separate namespaces. Picked the
        # same way as ssh_port; emitted into qemu's -netdev
        # unconditionally so the qemu cmdline doesn't have to know
        # which role's running.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((SSH_HOST, 0))
            self.wan_tcp_test_port = s.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind((SSH_HOST, 0))
            self.wan_udp_test_port = s.getsockname()[1]
        for guest_port in self._extra_guest_ports:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((SSH_HOST, 0))
                self.extra_hostfwd_ports[guest_port] = s.getsockname()[1]

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

        if self.keep_vm and not self._display_window:
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
                backing_fmt="qcow2",
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
            # ZFS variants pick a packer image (box/pug/lab), overlay
            # every disk packer staged (rpool + extra pools), and attach
            # any extra empty qcow2s on top. See AGENTS.md "Test
            # Environment Design".
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

            # Packer renames per-OS disks to packer-ubuntu-N.<format> in its
            # `extension` post-processor; mirror that suffix here. The format
            # matches arch (raw on Linux, qcow2 on Mac).
            os_src_paths = [
                f"{image_dir}/packer-ubuntu-{idx}.{self._packer_disk_format}" for idx in range(1, os_disk_count + 1)
            ]
            os_disk_paths: list[str] = []
            if self._commit_in_place:
                # No overlay: pass the source files straight to qemu in
                # their on-disk format. Writes persist in image_dir so
                # mise-tasks/packer/seed-deps.sh can publish it afterwards.
                os_disk_paths = list(os_src_paths)
                drive_format = self._packer_disk_format
            else:
                for idx, src in enumerate(os_src_paths, start=1):
                    dest = f"{self.workdir.name}/packer-ubuntu-{idx}"
                    await self._create_overlay(src, dest)
                    os_disk_paths.append(dest)
                drive_format = "qcow2"

            self.drives = [self._virtio_drive(path, drive_format) for path in os_disk_paths]
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

    async def _create_overlay(
        self, src: str, dest: str, size: str | None = None, backing_fmt: str | None = None
    ) -> None:
        """Create a qcow2 overlay pointing at *src* with optional resize.

        backing_fmt defaults to the packer artifact format, which is
        platform-dependent (raw on Linux, qcow2 on Mac). The minimal variant
        overrides it: its backing file is Ubuntu's cloud image, always qcow2
        regardless of host platform, so passing the packer format would tell
        qemu to read the qcow2 as raw and the guest would see a corrupt disk.

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
            backing_fmt or self._packer_disk_format,
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
                    )
                time.sleep(0.5)

    async def _ensure_minimal_cloudimg(self) -> Path:
        """Download (once) the Ubuntu minimal cloud image used by the `minimal` variant.

        Pulls through the lab Nexus raw proxy by default; `--upstream-mirrors`
        bypasses to cloud-images.ubuntu.com directly.
        """
        # Ubuntu publishes minimal-cloudimg arm64 only from noble onwards;
        # jammy is amd64-only. Fail loud rather than 404'ing on the curl.
        if self.arch.cloud_image_suffix == "arm64" and self.ubuntu_name == "jammy":
            raise RuntimeError(
                f"Ubuntu does not publish a minimal-cloudimg for {self.ubuntu_name}/arm64. "
                "Use --ubuntu noble (or later) on arm64 hosts, "
                "or run --machine minimal on x86_64."
            )
        name = f"ubuntu-{self.ubuntu_version}-minimal-cloudimg-{self.arch.cloud_image_suffix}.img"
        cache = self.imagedir / "cloud-images"
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / name
        if target.exists():
            return target

        base = (
            "https://cloud-images.ubuntu.com"
            if self.upstream_mirrors
            else "https://nexus.lab.fahm.fr/repository/ubuntu-cloud-images"
        )
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

    def _virtio_drive(self, path: str, format: str = "qcow2") -> str:
        """Return a virtio drive string with sensible cache/discard flags."""

        aio = "io_uring" if platform.system() == "Linux" else "threads"
        return f"file={path},if=virtio,cache=unsafe,aio={aio},discard=unmap,format={format},detect-zeroes=unmap"

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

    def _netdev_args(self) -> tuple[str, str]:
        """Return the (`-netdev` value, `-device` value) for the NIC backend.

        passt: qemu attaches via a `stream` netdev connected to the sidecar's
        unix socket (qemu >= 7.2). slirp: the legacy user-mode net with three
        hostfwds. The forward *set* is identical under both backends -- SSH
        for ansible-playbook plus TCP :32400 / UDP :51820 for the iptables
        `_verify` probes that `delegate_to: localhost` -- passt expresses them
        in _passt_command, slirp as hostfwd here.
        """
        if self._net_backend == "passt":
            assert self._passt_socket is not None
            netdev = f"stream,id=net0,server=off,addr.type=unix,addr.path={self._passt_socket}"
            return netdev, "virtio-net,netdev=net0"
        # Ports pre-picked in prepare(). qemu_user_net_args pins the VM's eth0
        # to network.hosts[inventory_host].physical (10.234.x test view); it's
        # empty for machines absent from the topology (minimal -> slirp's
        # default 10.0.2.0/24). Keyed on inventory_host (box), not machine
        # (box_deps), because the topology indexes inventory names.
        extra_fwds = "".join(
            f",hostfwd=tcp:{SSH_HOST}:{host_port}-:{guest_port}"
            for guest_port, host_port in self.extra_hostfwd_ports.items()
        )
        netdev = (
            f"user,id=user.0,"
            f"hostfwd=tcp:{SSH_HOST}:{self.ssh_port}-:22,"
            f"hostfwd=tcp:{SSH_HOST}:{self.wan_tcp_test_port}-:32400,"
            f"hostfwd=udp:{SSH_HOST}:{self.wan_udp_test_port}-:51820"
            f"{extra_fwds}"
            f"{qemu_user_net_args(self.inventory_host)}"
        )
        return netdev, "virtio-net,netdev=user.0"

    def _passt_command(self) -> list[str]:
        """passt sidecar argv. NATs the guest's egress and forwards the same
        three controller-side ports back to the guest as slirp's hostfwd does,
        but with passt's robust UDP datapath instead of libslirp.
        """
        assert self._passt_socket is not None
        return [
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
            # 127.0.0.1-bound forwards matching slirp's hostfwd set, so the
            # harness keeps connecting at SSH_HOST:<port>. The `addr/` prefix
            # binds the whole comma-list -- repeating it (addr/a,addr/b) is an
            # "Invalid port specifier" to passt, so the address appears once.
            "--tcp-ports",
            f"{SSH_HOST}/{self.ssh_port}:22,{self.wan_tcp_test_port}:32400"
            + "".join(f",{host_port}:{guest_port}" for guest_port, host_port in self.extra_hostfwd_ports.items()),
            "--udp-ports",
            f"{SSH_HOST}/{self.wan_udp_test_port}:51820",
            *passt_address_args(self.inventory_host),
        ]

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
        passt_log = OUT_DIR / f"{self.machine}.{self.ubuntu_name}.{self.role}.passt.ansi"
        with passt_log.open("wb") as handle:
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
                raise RuntimeError(f"passt exited before creating its socket; see {passt_log}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"passt socket {self._passt_socket} not created within 10s")
            await sleep_tick()

    async def boot(self) -> None:
        """Bring up the passt sidecar (if any), then launch qemu."""
        await self._start_passt()
        await super().boot()

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
            # to the built-in EHCI/UHCI for absolute-coordinate mouse.
            # aarch64 virt has no default graphics or input devices, so it
            # needs the full virtio-gpu + xhci + usb-kbd set; both come from
            # ArchProfile.keep_vm_extra_devices.
            display_backend = "cocoa" if platform.system() == "Darwin" else "gtk"
            display_args = [
                "-display",
                (
                    display_backend
                    if self._display_window
                    # Display number pre-picked in prepare(); qemu binds to
                    # 5900+display so the user can connect at 127.0.0.1:<port>.
                    else f"vnc=:{self.vnc_display}"
                ),
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
            await self._stop_passt()
            await super().stop()

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
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                async with asyncio.timeout(5):
                    await proc.wait()
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        # Drop the socket's private tmpdir once passt is gone (it unlinks the
        # socket itself on exit; this clears the parent). Runs whether or not
        # passt had already self-exited via --one-off.
        if self._passt_socket_dir is not None:
            self._passt_socket_dir.cleanup()
            self._passt_socket_dir = None

    async def _find_ssh_port(self) -> None:
        """Port was pre-picked in prepare(); nothing to discover at boot."""
        return
