import contextlib
import errno
import fcntl
import ipaddress
import json
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import ansible_mitogen
import yaml
from matrix import UBUNTU_RELEASES
from utils import (
    CheckFailedException,
    CommandFailedException,
    CommandResult,
    IdempotenceFailedException,
    colorize,
    failed_tasks,
    handle_interrupts,
    interrupts_held,
    log_line,
    phase,
    print_cmd_line,
    print_line,
    report_failure,
    run_command,
    sleep_tick,
    stop_process_group,
    task_title,
    tee_output,
)

OUT_DIR = Path("test/out")

SSH_KEY = "packer/vagrant.key"
# Loopback endpoint for qemu's hostfwds, VNC, SSH, and delegated WAN probes.
# qemu binds each forward to a free port itself (port 0), so parallel cells on
# one host never collide; the harness reads the chosen ports back over QMP.
SSH_HOST = "127.0.0.1"
PIDFILE_NAME = "pid"
# One line of `info usernet` per hostfwd: protocol, fd, host address and port,
# guest address and port.
_HOSTFWD_RE = re.compile(r"(TCP|UDP)\[HOST_FORWARD\]\s+\d+\s+\S+\s+(\d+)\s+\S+\s+(\d+)")
# Bound on reaching qemu's QMP socket and on each reply. qemu writes its
# pidfile before it opens the monitor, so the first connects can be refused.
QMP_TIMEOUT = 5
# How long stop() lets qemu shut down on SIGTERM before SIGKILL.
STOP_GRACE_SECONDS = 5


TOPOLOGY_PATH = Path(__file__).parent.parent / "data" / "network_topology.yml"
GUEST_JOURNAL_UNIT_PATH = Path(__file__).parent / "homelab_guest_journal.service"
# Guest machine, NIC, cloud-image token, and UEFI pairs per architecture,
# shared with the Packer fixture build and the CI image stores.
ARCHITECTURES = yaml.safe_load((Path(__file__).parent.parent / "data" / "architectures.yml").read_text())
# -device flags every guest gets. aarch64 virt has no default graphics, and
# without a framebuffer Ubuntu's initramfs init-top/framebuffer script falls
# back to `sleep 1` + vesafb on every boot. q35's std VGA already covers x86_64.
GUEST_DEVICES = {
    "x86_64": (),
    "aarch64": ("-device", "virtio-gpu-pci"),
}
# Extra -device flags for interactive (VNC) mode. q35 brings PS/2 / ICH9 USB,
# so x86_64 only needs usb-tablet for an absolute mouse; aarch64 virt has no
# default input.
KEEP_VM_DEVICES = {
    "x86_64": ("-device", "usb-tablet"),
    "aarch64": ("-device", "qemu-xhci", "-device", "usb-kbd", "-device", "usb-tablet"),
}


def host_arch() -> str:
    """This host's platform.machine(), as a data/architectures.yml key."""
    machine = platform.machine()
    arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    if arch not in ARCHITECTURES:
        raise RuntimeError(f"Unsupported host architecture: {machine}")
    return arch


# Guest ports the controller-side firewall _verify probes reach as WAN traffic;
# qemu forwards each from a free port on the cell's loopback.
DEFAULT_WAN_FORWARDS: dict[str, tuple[int, ...]] = {
    "tcp": (
        18080,  # published-container DNAT fixture
        9092,  # Authelia forward-auth negative WAN-source probe
    ),
    "udp": (
        51820,  # WireGuard
        5353,  # mDNS negative WAN-source probe
        41641,  # Tailscale direct underlay
    ),
}

# Pinned through ANSIBLE_CONFIG (ansible_env): ansible silently ignores an
# ansible.cfg in a world-writable cwd, which the GitLab CI checkout is, and
# without it the first connect to a fresh cell fails host key verification.
ANSIBLE_CONFIG_PATH = Path(__file__).parent.parent / "ansible.cfg"
# The venv's mitogen strategy plugins, passed straight to ansible so a run
# never depends on (or rewrites) the repo's .ansible-mitogen-strategy symlink.
MITOGEN_STRATEGY_DIR = Path(ansible_mitogen.__file__).parent / "plugins" / "strategy"


def _load_test_topology() -> dict:
    """Load data/network_topology.yml with the 10.123 → 10.234 gsub
    applied. The test harness always uses the test view regardless of
    which machine is selected — `test/inventory.ini` puts every
    machine (minimal/lab) in the [test] group, so ansible
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
    # slot (.0.2-.0.9) and every per-VLAN host block (.X.128-.255),
    # so the qemu router never collides with a topology-claimed address.
    host_ip = str(net.broadcast_address - 1)
    dns_ip = str(net.broadcast_address - 2)
    return f",net={supernet},host={host_ip},dns={dns_ip},dhcpstart={physical}"


class QemuMachineSpec(NamedTuple):
    ssh_user: str
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
        cloud_image=True,
        memory_mb=2048,
        vcpus=2,
    ),
    "lab": QemuMachineSpec(
        ssh_user="vagrant",
        # lab: matches the lab prod host. mdadm-EFI + mdadm-swap +
        # 3-disk mirror rpool + dozer + tank + mouse, all baked in.
        # Default integration fixture and promoted CI image.
    ),
}


_PACKER_DISK_RE = re.compile(r"packer-ubuntu-(\d+)\.(raw|qcow2)")


def _minimal_user_data() -> str:
    """The minimal fixture's cloud-init user-data with the journal mirror unit
    the Packer fixtures bake, so both install the same file."""
    user_data = yaml.safe_load((Path(__file__).parent / "minimal" / "user-data").read_text())
    user_data["write_files"] = [
        {
            "path": "/etc/systemd/system/homelab_guest_journal.service",
            "permissions": "0644",
            "content": GUEST_JOURNAL_UNIT_PATH.read_text(),
        }
    ]
    return "#cloud-config\n" + yaml.safe_dump(user_data, sort_keys=False)


def link_packer_artifacts(published: Path, dest: Path) -> None:
    """Hardlink one complete version of *published*'s artifacts into *dest*.

    packer:build and the CI hydrate both publish by renaming the current
    directory away, renaming the new one into place, and only then deleting the
    old one. Linking relative to one open directory fd keeps every link from a
    single version; the path still naming that directory afterwards proves it
    was never renamed away, so nothing in it was deleted mid-link. Otherwise
    the swap happened under us and the next attempt takes the new version.
    Hardlinks need *published* on *dest*'s filesystem (EXDEV otherwise), and,
    for files another user owns, group write under fs.protected_hardlinks.
    """
    for _ in range(ARTIFACT_LINK_ATTEMPTS):
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir()
        try:
            dir_fd = os.open(published, os.O_RDONLY | os.O_DIRECTORY)
        except FileNotFoundError:
            # Between a publish's two renames, the path briefly names nothing.
            sleep_tick()
            continue
        try:
            for name in os.listdir(dir_fd):
                if name == "efivars.fd" or name.startswith("packer-ubuntu-"):
                    os.link(name, dest / name, src_dir_fd=dir_fd)
            current = os.stat(published)
            linked = os.fstat(dir_fd)
            if (current.st_dev, current.st_ino) == (linked.st_dev, linked.st_ino):
                return
        except FileNotFoundError:
            # The directory was renamed away and its files deleted mid-link.
            pass
        finally:
            os.close(dir_fd)
        sleep_tick()
    raise RuntimeError(f"{published} kept changing; could not link one version in {ARTIFACT_LINK_ATTEMPTS} attempts")


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
    append: str = ""
    foreground: bool = False
    display_window: bool = False


SSH_WAIT_TIMEOUT = 120
# Boot-wait polling. The guest reaches sshd a few seconds in, so a 1s poll
# would add up to a second per boot.
BOOT_POLL_INTERVAL = 0.1

# Bound on `systemctl is-system-running --wait`, which otherwise waits for as
# long as any unit is still activating and leaves only the overall session
# timeout to end a wedged start.
SYSTEM_RUNNING_WAIT_TIMEOUT = 600
IDFILE_TIMEOUT = 60
# Bounded retries for hardlinking one published image version; each retry
# follows a publish or hydrate swapping the directory mid-link.
ARTIFACT_LINK_ATTEMPTS = 20
# Bounded exclusive-acquire window on the per-image cloud-image download lock.
# The holder keeps it across the curl, so a waiter must outlast a full download
# of a few-hundred-MB image off a slow mirror; bounded so a wedged downloader
# surfaces rather than hanging every concurrent minimal cell.
CLOUDIMG_LOCK_TIMEOUT = 600


class Machine:
    """Start disposable QEMU guests for role-level integration tests.

    Deliberately synchronous: a cell is one VM driven by one sequence of
    subprocesses. The session deadline is enforced by giving every blocking
    call what is left of it (remaining), not by interrupting from outside.
    """

    output_file: Path
    journal_file: Path
    boot_file: Path
    workdir: tempfile.TemporaryDirectory[str]
    workdir_path: Path
    # fd of <workdir>/.live, held with fcntl.LOCK_EX|LOCK_NB for the lifetime
    # of the Machine. Liveness signal consumed by sweep_stale_workdirs(): the
    # kernel releases the lock on process death (clean or SIGKILL/OOM), so a
    # crashed run's workdir becomes reapable without a polling daemon.
    _live_lock_fd: int
    # Controller-side WAN probe endpoint, so `delegate_to: localhost`
    # probes in roles/firewall's _verify can exercise rules keying on the
    # WAN interface (traffic originating inside the VM never ingresses on
    # the WAN iface). qemu forwards them from free loopback ports, mapped to
    # guest ports in wan_forward_ports once the VM is up.
    wan_forward_ports: dict[str, dict[str, int]]

    drives: list[str]
    # The VNC port qemu picked for a kept VM without a local display window.
    vnc_port: int | None

    def __init__(
        self,
        machine: str,
        role: str,
        keep_vm: bool,
        ubuntu_name: str,
        machine_timeout: int,
        upstream_mirrors: bool = False,
        *,
        launch: LaunchOptions | None = None,
        vcpus: int | None = None,
        memory_mb: int | None = None,
        quiet_ansible: bool = False,
    ):
        """QEMU-backed machine wrapper used by integration tests.

        launch carries launch.py-only qemu overrides. vcpus and memory_mb
        override the machine spec; quiet_ansible trims playbook output to
        changes and failures.
        """
        self.launch = launch or LaunchOptions()
        self.quiet_ansible = quiet_ansible
        spec = QEMU_MACHINE_SPECS[machine]
        spec = spec._replace(vcpus=vcpus or spec.vcpus, memory_mb=memory_mb or spec.memory_mb)

        self.imagedir: Path = imagedir_for_host()

        self._spec = spec
        # Captured once at construction so prepare()/_boot_command() don't
        # have to re-run platform.machine() on every access.
        self.arch = host_arch()
        self.guest: dict = ARCHITECTURES[self.arch]["guest"]
        self.qemu_binary = f"qemu-system-{self.arch}"

        self.ssh_port = 0
        self.ssh_host = SSH_HOST
        self.vnc_port = None
        self.qmp_socket = Path(f"/tmp/homelab-qmp-{os.getpid()}")
        self.ssh_user = spec.ssh_user
        self.machine = machine
        self.role = role
        self.keep_vm = keep_vm
        self.ubuntu_name = ubuntu_name
        self.machine_timeout = machine_timeout
        self.upstream_mirrors = upstream_mirrors
        self.proc: subprocess.Popen[bytes] | None = None
        # Monotonic time the session must finish by; None for no limit.
        self.deadline: float | None = None
        self._live_lock_fd = -1
        self._ansible_staged = False
        self._last_ansible_cmd: tuple[str, ...] | None = None
        self.wan_forward_ports = {"tcp": {}, "udp": {}}

        prefix = f"{self.machine}.{self.ubuntu_name}.{self.role}"
        output_dir = OUT_DIR
        output_dir.mkdir(parents=True, exist_ok=True)
        self.output_file = output_dir / f"{prefix}.output.ansi"
        self.journal_file = output_dir / f"{prefix}.journal.ansi"
        self.boot_file = output_dir / f"{prefix}.boot.ansi"
        # What report_failure showed for a failed run; test:all reprints it.
        self.failure_file = output_dir / f"{prefix}.failure.ansi"
        self._artifact_files = (self.output_file, self.journal_file, self.boot_file, self.failure_file)
        for stale in self._artifact_files:
            stale.unlink(missing_ok=True)
        # The workdir lands alongside the packer artifacts, on the same
        # filesystem, so prepare() can hardlink the disks it boots from.
        self.workdir = tempfile.TemporaryDirectory(dir=self.imagedir)
        self.workdir_path = Path(self.workdir.name)
        # Claim the liveness lock immediately after the workdir exists so a
        # concurrent sweep can't reap the dir between mkdtemp and the first
        # qemu/ansible spawn. LOCK_NB so a contended
        # lock fails loudly (would only happen if two Machines somehow shared
        # a workdir, which mkdtemp prevents -- a BlockingIOError here is a
        # bug, not a race).
        self._live_lock_fd = os.open(self.workdir_path / ".live", os.O_WRONLY | os.O_CREAT, 0o644)
        fcntl.flock(self._live_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # git can't store mode 0600, and ssh rejects a private key with looser
        # permissions.
        ssh_key = Path(SSH_KEY)
        if ssh_key.exists():
            ssh_key.chmod(0o600)

    @property
    def pid_file(self) -> Path:
        """QEMU pidfile path under the per-run workdir."""
        return self.workdir_path / PIDFILE_NAME

    @property
    def ssh_control_path(self) -> str:
        """Stable ControlMaster socket path shared by every connection to this cell.

        One socket per cell, reused by the harness's own ssh AND by every
        ansible-playbook phase, so phases 2..N skip the SSH handshake + agent
        round-trip + mitogen interpreter bootstrap. Keyed on the cell's SSH
        port, which qemu holds for the VM's lifetime. Lives in /tmp (writable,
        short) rather than the per-cell workdir: workdir lands on /mnt/scratch
        on Linux CI hosts, and a unix socket path must stay under ~104 chars.
        """
        return f"/tmp/homelab-cm-{self.ssh_port}"

    def _ssh_options(self) -> list[str]:
        """Return the shared `-o flag=value` pairs for harness SSH commands."""
        return [
            "-o",
            f"ControlPath={self.ssh_control_path}",
            # auto: the first connection, harness or ansible, opens the cell's
            # master and every later one reuses it. ControlPersist keeps it
            # warm between phases. Matches ANSIBLE_SSH_ARGS so both sides land
            # on the same socket.
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
            "ANSIBLE_STRATEGY_PLUGINS": str(MITOGEN_STRATEGY_DIR),
            # Override [ssh_connection] ssh_args wholesale so ansible pins its
            # ControlPath to the cell-stable socket (ssh_control_path) instead
            # of its default per-invocation path. Without an explicit
            # ControlPath, each of the ~6 ansible-playbook processes opens its
            # own master; sharing one keeps the socket hot across phases. The
            # rest of the flags mirror ansible.cfg verbatim (ControlMaster,
            # ControlPersist, UserKnownHostsFile, ForwardAgent) so this doesn't
            # regress any of them; the harness's own ssh shares the same path.
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
            # Timestamps each TASK header and ends every playbook run with a
            # recap of its slowest tasks.
            "ANSIBLE_CALLBACKS_ENABLED": "ansible.posix.profile_tasks",
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
        if self.quiet_ansible:
            env["ANSIBLE_DISPLAY_OK_HOSTS"] = "false"
            env["ANSIBLE_DISPLAY_SKIPPED_HOSTS"] = "false"
            env["ANSIBLE_VERBOSITY"] = "0"
            # Per-task timestamps would print without their suppressed
            # ok/skipped TASK headers; keep only the end-of-playbook recap,
            # uncut so the long tail of small tasks stays measurable.
            env["PROFILE_TASKS_SUMMARY_ONLY"] = "true"
            env["PROFILE_TASKS_TASK_OUTPUT_LIMIT"] = "all"

        return env

    @property
    def in_aws(self) -> bool:
        """Whether this cell's guest egresses through AWS.

        Cloud-environment choices key on this -- the in-region EC2 apt/ECR
        mirrors are reachable while the LAN Nexus and AdGuard VIP are not.
        Driven by HOMELAB_TEST_IN_AWS: set by the aws_qemu CI cell (a qemu
        guest on an AWS shell runner), unset for local/lab qemu.
        """
        return os.environ.get("HOMELAB_TEST_IN_AWS", "").strip().lower() in ("1", "true", "yes")

    def format_ansible_cmd(self, *cmd: str) -> list[str]:
        """Build an ansible-playbook command pinned to this machine's SSH details.

        ANSIBLE_* env vars come back from ansible_env() and are passed to
        run_command via env=, not prepended to argv.
        """
        parts = [
            "ansible-playbook",
            # Static playbooks declare `hosts: all`; --limit pins the play to
            # the inventory host we actually provisioned.
            "--limit",
            self.machine,
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
            json.dumps({"wan_forward_ports": self.wan_forward_ports}, sort_keys=True, separators=(",", ":")),
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
            "--inventory",
            str(self.connection_inventory_path),
        ]
        if cmd:
            parts += cmd
        return parts

    def remaining(self) -> float | None:
        """Seconds left before the session deadline, or None without one.

        Raises a message-less TimeoutError once the deadline has passed; the
        harness reports that as the cell timing out.
        """
        if self.deadline is None:
            return None
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError()
        return left

    def ssh_command(self, *cmd: str, check: bool = True) -> CommandResult:
        """Execute an SSH command and stream output into the role log."""

        return run_command(self.format_ssh_cmd(*cmd), check=check, timeout=self.remaining())

    @property
    def connection_inventory_path(self) -> Path:
        """Per-cell inventory carrying this fixture's SSH connection vars."""
        # Its own subdirectory, so Ansible finds no group_vars/ or host_vars/
        # beside it and loads the staged copies only at playbook precedence.
        return self.workdir_path / "inventory" / "connection.ini"

    def _write_connection_inventory(self) -> None:
        """Set the fixture's SSH endpoint as inventory host vars, as hosts.ini does.

        Extra vars would outrank every other source, including the connection
        vars Ansible swaps in under delegate_to, and so hide lookups that only
        work for the -e form.
        """
        path = self.connection_inventory_path
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            f"{self.machine} ansible_ssh_host={self.ssh_host} ansible_ssh_port={self.ssh_port}"
            f" ansible_ssh_user={self.ssh_user} ansible_ssh_private_key_file={SSH_KEY}\n"
        )

    def ansible_command(self, *cmd: str, check: bool = True) -> CommandResult:
        """Execute ansible-playbook with machine-specific SSH overrides."""

        self._stage_ansible_controller()
        self._last_ansible_cmd = cmd
        return run_command(self.format_ansible_cmd(*cmd), check=check, env=self.ansible_env(), timeout=self.remaining())

    def _stage_ansible_controller(self) -> None:
        """Populate controller inputs on demand before the first Ansible run."""

        if self._ansible_staged:
            return

        self._write_connection_inventory()

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

        for playbook in Path("test/playbooks").glob("*.yml"):
            playbook.copy_into(self.workdir_path)
        self._ansible_staged = True

    def boot(self) -> None:
        """Launch qemu under a timeout wrapper."""

        cmd = self._boot_command()
        print_cmd_line(cmd)

        # Redirect both streams into a per-machine boot log so the chatty
        # systemd init / qemu console doesn't drown out the test transcript.
        # The kernel writes straight to disk, so no pipe buffer to drain, and
        # stderr=STDOUT keeps the on-disk order exactly the syscall order.
        # start_new_session=True puts the child in its own process group so
        # terminal SIGINT only hits the python parent; we drive child
        # shutdown explicitly through Machine.stop(). stdin=DEVNULL keeps
        # qemu's `-serial stdio` from competing with the terminal for
        # keystrokes.
        # Interrupts wait until self.proc is set, so stop() always sees qemu.
        with self.boot_file.open("wb") as handle, interrupts_held():
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True
            )

    def ensure_booted(self) -> None:
        """Block until qemu writes its pidfile or the launch fails."""

        deadline = time.monotonic() + IDFILE_TIMEOUT
        id_path = self.pid_file
        while not id_path.exists():
            if self.proc and (returncode := self.proc.poll()) is not None:
                raise RuntimeError(
                    f"Launching machine failed (qemu wrapper exited with {returncode}); see {self.boot_file}"
                )
            if time.monotonic() > deadline:
                raise TimeoutError(f"PID file {id_path} not created within {IDFILE_TIMEOUT}s")
            self.remaining()
            sleep_tick(BOOT_POLL_INTERVAL)
        self._read_host_ports()

    def _qmp_connect(self) -> socket.socket:
        """Connect to qemu's QMP socket, retrying until the monitor is up."""
        deadline = time.monotonic() + QMP_TIMEOUT
        while True:
            sock = socket.socket(socket.AF_UNIX)
            sock.settimeout(QMP_TIMEOUT)
            try:
                sock.connect(str(self.qmp_socket))
                return sock
            except FileNotFoundError, ConnectionRefusedError:
                sock.close()
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)

    def _qmp(self, command: str, arguments: dict | None = None) -> object:
        """Run one QMP command against this VM and return its result."""
        deadline = time.monotonic() + QMP_TIMEOUT
        with self._qmp_connect() as sock:
            replies = sock.makefile("rb")
            for request in ({"execute": "qmp_capabilities"}, {"execute": command, "arguments": arguments or {}}):
                sock.sendall(json.dumps(request).encode() + b"\n")
                # The greeting and asynchronous events carry no "return". The
                # socket timeout bounds each read, not a steady run of events.
                while not ({"return", "error"} & (message := json.loads(replies.readline())).keys()):
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"QMP {request['execute']} got no reply within {QMP_TIMEOUT}s")
                if "error" in message:
                    raise RuntimeError(f"QMP {request['execute']} failed: {message['error']}")
            return message["return"]

    def _read_host_ports(self) -> None:
        """Learn the loopback ports qemu bound for SSH, the WAN probes, and VNC."""
        usernet = self._qmp("human-monitor-command", {"command-line": "info usernet"})
        forwards = {(proto.lower(), guest): int(host) for proto, host, guest in _HOSTFWD_RE.findall(str(usernet))}
        self.ssh_port = forwards[("tcp", "22")]
        self.wan_forward_ports = {
            proto: {str(port): forwards[(proto, str(port))] for port in ports}
            for proto, ports in DEFAULT_WAN_FORWARDS.items()
        }
        if self.keep_vm and not self.launch.display_window:
            vnc = self._qmp("query-vnc")
            assert isinstance(vnc, dict)
            self.vnc_port = int(vnc["service"])

    def ensure_ssh(self) -> None:
        """Wait for the daemon banner on the port qemu forwards to the guest's sshd."""

        deadline = time.monotonic() + SSH_WAIT_TIMEOUT
        while not self._ssh_banner_ready():
            if time.monotonic() > deadline:
                raise TimeoutError("SSH daemon did not become ready in time")
            self.remaining()
            sleep_tick(BOOT_POLL_INTERVAL)

    def wait_system_running(self) -> tuple[int, str]:
        """Wait, bounded, for systemd to stop starting units; return (rc, state).

        rc is 124 when the bound expired with units still activating.
        """
        result = self.ssh_command(
            "timeout", str(SYSTEM_RUNNING_WAIT_TIMEOUT), "systemctl", "is-system-running", "--wait", check=False
        )
        return result.exitcode, "\n".join(result.stdout).strip()

    def failed_units(self) -> str:
        """The guest's failed units, one per line, or "(none)"."""
        failed = self.ssh_command("systemctl", "--failed", "--no-legend", check=False)
        return "\n".join(failed.stdout).rstrip() or "(none)"

    def ensure_system_running(self) -> None:
        """Require systemd to finish booting in a healthy running state."""
        rc, state = self.wait_system_running()
        if rc == 0 and state == "running":
            print_line(f"System fully booted: {state}")
            return
        raise RuntimeError(f"System reached state {state!r} (rc={rc}); failed units:\n{self.failed_units()}")

    def ensure_cloud_init(self) -> None:
        """Block until cloud-init's config and final stages finish.

        SSH opens during cloud-init's network stage. Waiting here prevents the
        first converge from racing its package locks and /etc/hosts rewrite.
        A degraded-but-complete run may return non-zero, so the result is not a
        gate.
        """
        self.ssh_command("sudo", "cloud-init", "status", "--wait", check=False)

    def _ssh_banner_ready(self) -> bool:
        """Probe the SSH port once. Return True iff a non-empty banner arrives."""

        # qemu's hostfwd accepts before sshd listens, then sends nothing or
        # drops the connection, so only a banner proves sshd is up. OSError
        # covers the refused, reset, and timed-out cases alike. The timeout
        # stays short: a probe opened before the guest's network is up waits
        # on slirp's SYN retransmit backoff, not on sshd, so a fresh probe
        # sees sshd sooner.
        try:
            with socket.create_connection((self.ssh_host, self.ssh_port), timeout=0.3) as sock:
                return bool(sock.recv(1024).strip())
        except OSError:
            return False

    def run(self, test: Callable[[], None], label: str | None) -> int:
        """Run an entry point's *test* with output mirrored to the run log.

        Returns the harness exit code: 0 passed, 1 failed, 124 timed out, 125
        not idempotent, 130 interrupted. A pass deletes the per-run logs;
        anything else keeps them for the post-mortem. Failed Ansible tasks
        are shown on their own, otherwise the transcript's tail; the verdict
        comes last, so it is the line `test:all` leaves on screen. The
        verdict names *label* unless it is None, for a console whose tag
        already names the cell.
        """
        subject = f"{label} " if label else ""
        rc = 0
        verdict = ""
        excerpt: list[str] = []
        # SIGTERM (GNU parallel's --termseq, a CI cancel) stops the cell the
        # way Ctrl-C does, through stop().
        handle_interrupts()
        with tee_output(self.output_file):
            try:
                test()
            except IdempotenceFailedException as exc:
                print_line(str(exc), error=True)
                verdict = "not idempotent"
                rc = 125
            except CommandFailedException as exc:
                failed = failed_tasks(exc.stdout)
                excerpt = [line for block in failed for line in block]
                if failed:
                    # The tasks say what failed; the command line and its
                    # stderr tail would only bury them.
                    log_line(str(exc), error=True)
                    verdict = f"failed at {task_title(failed[-1][0])}"
                else:
                    print_line(str(exc), error=True)
                    verdict = "failed"
                rc = 1
            except CheckFailedException as exc:
                print_line(str(exc), error=True)
                verdict = "failed"
                rc = 1
            except TimeoutError as exc:
                # The session deadline raises a message-less TimeoutError; the
                # phase guards (ensure_booted, ensure_ssh) carry their cause,
                # so a slow boot-to-sshd isn't misread as the overall timeout.
                if str(exc):
                    print_line(str(exc), error=True)
                deadline = f" after {self.machine_timeout}s" if self.machine_timeout else ""
                verdict = f"timed out{deadline}"
                rc = 124  # GNU `timeout`'s exit code for "command timed out"
            except KeyboardInterrupt:
                print_line("\nInterrupted, shutting down...")
                verdict = "interrupted"
                rc = 130
            except Exception:
                # The traceback would otherwise go straight to stderr,
                # bypassing tee_output, so the run log would miss it.
                print_line(traceback.format_exc().rstrip(), error=True)
                verdict = "crashed"
                rc = 1
            if rc != 0:
                report_failure(excerpt, self.output_file, self.failure_file)
                print_line(f"✗ {subject}{verdict}", error=True)
        if rc == 0:
            print_line(colorize(f"✓ {subject}passed", "green"))
            for path in self._artifact_files:
                path.unlink(missing_ok=True)
        return rc

    def wait(self, timeout: float | None = None) -> None:
        """Wait for qemu to exit; subprocess.TimeoutExpired past *timeout*,
        or the session's TimeoutError if its deadline comes first."""
        if not self.proc:
            return
        left = self.remaining()
        if left is None or (timeout is not None and timeout <= left):
            self.proc.wait(timeout)
            return
        try:
            self.proc.wait(left)
        except subprocess.TimeoutExpired:
            raise TimeoutError() from None

    @contextlib.contextmanager
    def session(self, timeout: int | None) -> Iterator[None]:
        """Prepare and boot the VM, run the body, and always stop the VM.

        *timeout* sets the deadline every blocking call is bounded by (see
        remaining), and a body that finishes past it still times out. stop()
        runs whatever happened, including an interrupt during prepare or
        boot. A kept VM stays up after the body -- passed, failed, or timed
        out -- until Ctrl-C, with no deadline; only Ctrl-C skips that hold.
        """
        self.deadline = time.monotonic() + timeout if timeout else None
        try:
            with phase("prepare"):
                self.prepare()
                self.boot()
            try:
                yield
                self.remaining()
            except BaseException as exc:
                if self.keep_vm and not isinstance(exc, KeyboardInterrupt):
                    if isinstance(exc, TimeoutError) and self.deadline and time.monotonic() >= self.deadline:
                        print_line(f"Timed out after {timeout}s; --keep set, dropping to SSH for debug")
                    self.deadline = None
                    self.print_ssh_instructions()
                    self.wait()
                raise
            if self.keep_vm:
                self.deadline = None
                self.print_ssh_instructions()
                self.wait()
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop qemu and free this run's temporary resources.

        Signals qemu's whole process group: boot() starts it in its own
        session, so the group holds the `timeout` wrapper and qemu, and still
        holds qemu after the wrapper dies or before qemu writes its pidfile.
        SIGTERM lets qemu exit cleanly; SIGKILL follows after
        STOP_GRACE_SECONDS (stop_process_group). Ctrl-C and SIGTERM are
        dropped for the duration, so a second one (or a parallel --termseq)
        can't cut cleanup short, and each cleanup step runs even if an
        earlier one fails.
        """
        with interrupts_held(redeliver=False):
            try:
                with contextlib.suppress(OSError):  # a transcript that cannot take the line must not cost the cleanup
                    print_line("Stopping machine...")
                if self.proc:
                    stop_process_group(self.proc, grace_seconds=STOP_GRACE_SECONDS)
            finally:
                try:
                    self._close_ssh_master()
                finally:
                    # Release the liveness lock before rmtree -- the kernel
                    # would release it on close()/exit anyway, but doing it
                    # explicitly keeps the ordering obvious.
                    if self._live_lock_fd >= 0:
                        with contextlib.suppress(OSError):  # already closed; nothing left to release
                            os.close(self._live_lock_fd)
                        self._live_lock_fd = -1
                    try:
                        self.qmp_socket.unlink(missing_ok=True)
                    finally:
                        self.workdir.cleanup()

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
            print_line(f"VNC: {self.ssh_host}:{self.vnc_port}")

    def prepare(self) -> None:
        """Create overlay images and seed data for the selected template."""

        if self._spec.cloud_image:
            cloud_image = self._ensure_minimal_cloudimg()
            seed_img = self.workdir_path / "seed.img"
            disk_img = self.workdir_path / "disk.img"
            user_data = self.workdir_path / "user-data"
            user_data.write_text(_minimal_user_data())
            run_command(
                [
                    "xorrisofs",
                    "-output",
                    str(seed_img),
                    "-volid",
                    "cidata",
                    "-joliet",
                    "-rock",
                    str(user_data),
                    "test/minimal/meta-data",
                ],
                timeout=self.remaining(),
            )
            self._create_overlay(
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
            if self.arch != "x86_64":
                self.drives += self._uefi_drives()
        else:
            # Artifact-backed variants overlay every disk Packer published,
            # through private hardlinks: qemu-img records the backing path by
            # value, and a later publish or hydrate must not swap it out from
            # under this run.
            if self.launch.image_dir is not None:
                published = self.launch.image_dir.resolve()
            else:
                published = self.imagedir / self.ubuntu_name / self.machine
            image_dir = self.workdir_path / "base"
            link_packer_artifacts(published, image_dir)
            os_src_paths, artifact_format = discover_packer_disks(image_dir)

            os_disk_paths: list[str] = []
            for idx, src in enumerate(os_src_paths, start=1):
                dest = self.workdir_path / f"packer-ubuntu-{idx}"
                self._create_overlay(str(src), str(dest), backing_fmt=artifact_format)
                os_disk_paths.append(str(dest))

            self.drives = [self._virtio_drive(path) for path in os_disk_paths]
            shutil.copyfile(image_dir / "efivars.fd", self.workdir_path / "efivars.fd")
            self.drives += self._uefi_drives()

    def _create_overlay(self, src: str, dest: str, *, backing_fmt: str, size: str | None = None) -> None:
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
        run_command(args, timeout=self.remaining())

    def _ensure_minimal_cloudimg(self) -> Path:
        """Download and cache the Ubuntu minimal cloud image.

        Local runs use the Nexus proxy by default; AWS cells and
        --upstream-mirrors fetch directly from cloud-images.ubuntu.com.
        """
        name = f"ubuntu-{UBUNTU_RELEASES[self.ubuntu_name]}-minimal-cloudimg-{self.guest['cloud_image_suffix']}.img"
        cache = self.imagedir / "cloud-images"
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / name
        if target.exists():
            return target

        # Several cells share this cache; poll so the wait stays bounded.
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
                self.remaining()
                time.sleep(0.5)
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
            run_command(["curl", "-fL", "--retry", "3", "-o", str(tmp), url], timeout=self.remaining())
            os.replace(tmp, target)
            return target
        finally:
            os.close(fd)

    def _virtio_drive(self, path: str) -> str:
        """Return a virtio drive string with sensible cache/discard flags."""

        aio = "io_uring" if platform.system() == "Linux" else "threads"
        return f"file={path},if=virtio,cache=unsafe,aio={aio},discard=unmap,format=qcow2,detect-zeroes=unmap"

    def _uefi_drives(self) -> list[str]:
        """Return the auto-detected UEFI code and writable vars pair.

        The CODE blob is the host OS's pair in data/architectures.yml. The VARS blob is
        one of:

        - {workdir}/efivars.fd, copied from the packer image for ZFS
          variants so bootloader entries survive across runs;
        - else a fresh empty file sized to the code blob.

        qemu pflash requires CODE and VARS to be the same size, and EDK2 builds
        aren't uniform: aarch64 EDK2 ships at 64 MiB, x86_64 OVMF typically at
        4 MiB.
        """
        host_os = platform.system().lower()
        try:
            code_path = Path(self.guest["uefi_firmware"][host_os]["code"])
        except KeyError:
            raise RuntimeError(f"No {self.arch} UEFI firmware is defined for {host_os} hosts") from None
        packer_vars = self.workdir_path / "efivars.fd"
        if packer_vars.exists():
            vars_path = packer_vars
        else:
            vars_path = self.workdir_path / "uefi-vars.fd"
            with vars_path.open("wb") as handle:
                handle.truncate(code_path.stat().st_size)
        return [
            f"file={code_path},if=pflash,unit=0,format=raw,readonly=on",
            f"file={vars_path},if=pflash,unit=1,format=raw",
        ]

    def _netdev_args(self) -> tuple[str, str]:
        """Return the (`-netdev` value, `-device` value) for qemu's user-mode net.

        The hostfwds carry SSH for ansible-playbook plus wan_forward_ports for
        the firewall `_verify` probes that `delegate_to: localhost`.
        """
        # Host port 0 lets qemu bind a free port itself; ensure_booted reads
        # them back. qemu_user_net_args pins the VM's eth0 to
        # network.hosts[machine].physical (10.234.x test view); it is empty for
        # minimal, which has no topology identity.
        hostfwds = [f"hostfwd=tcp:{self.ssh_host}:0-:22"]
        for proto, guest_ports in DEFAULT_WAN_FORWARDS.items():
            hostfwds.extend(f"hostfwd={proto}:{self.ssh_host}:0-:{guest_port}" for guest_port in guest_ports)
        netdev = f"user,id=user.0,{','.join(hostfwds)}{qemu_user_net_args(self.machine)}"
        return netdev, f"{self.guest['net_device']},netdev=user.0"

    def _boot_command(self) -> list[str]:
        """Assemble the qemu command line for the prepared disks.

        Arch- and OS-aware: data/architectures.yml supplies the machine type
        and NIC, GUEST_DEVICES and KEEP_VM_DEVICES the always-on and keep-VM
        device sets; this method only chooses accel based on
        platform.system(). Display hardware (virtio-gpu-pci + qemu-xhci) works
        identically on both arches.
        """
        accel = "hvf" if platform.system() == "Darwin" else "kvm"

        if self.keep_vm:
            # q35 has std VGA + PS/2 keyboard by default but USB is opt-in
            # (machine flag usb=on, applied below); usb-tablet then attaches
            # to the built-in EHCI/UHCI for absolute-coordinate mouse.
            # aarch64 virt has no default input devices, so it needs the
            # xhci + usb-kbd set from KEEP_VM_DEVICES.
            display_backend = "cocoa" if platform.system() == "Darwin" else "gtk"
            display_args = [
                "-display",
                (
                    display_backend
                    if self.launch.display_window
                    # The first free display from :0 up; read back via QMP.
                    else f"vnc={self.ssh_host}:0,to=99"
                ),
                *KEEP_VM_DEVICES[self.arch],
                "-k",
                "fr",
            ]
        else:
            display_args = ["-display", "none"]

        # A unified ZBM EFI image embeds its own initrd/cmdline PE sections
        # (read by qemu's PE loader when pflash/UEFI is attached, the same
        # LoadOptions-override rEFInd uses), so no -initrd is needed.
        direct_boot: list[str] = []
        if self.launch.kernel is not None:
            direct_boot = ["-kernel", str(self.launch.kernel.resolve()), "-append", self.launch.append]

        netdev_arg, net_device_arg = self._netdev_args()

        # Last-resort GNU timeout around qemu, outlasting machine_timeout (the
        # Python deadline) by 60s so the entry point's own rc=124 and
        # Machine.stop()'s graceful->SIGKILL escalation come first. A kept VM
        # runs until the operator stops it, and runs unwrapped: GNU timeout
        # detaches its child from the controlling tty, which would leave
        # --foreground's mon:stdio unusable.
        wrapper = [] if self.keep_vm else ["timeout", "--kill-after=10s", str(self.machine_timeout + 60)]
        cmd = [
            *wrapper,
            self.qemu_binary,
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
            f"type={self.guest['machine_type']},accel={accel},usb=on",
            # splash-time is the edk2 front-page timeout (fw_cfg
            # etc/boot-menu-wait); without it ArmVirtQemu waits its 5s
            # PcdPlatformBootTimeOut before every boot. OVMF reads the same
            # key.
            "-boot",
            "menu=on,splash-time=0",
            *GUEST_DEVICES[self.arch],
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
            # The console the image's journal-mirror unit follows the
            # journal onto -- the only diagnostic that survives a guest
            # which never reaches SSH, and the only one spanning the
            # reboots that wipe the fixture's volatile journal. virtio and
            # not a second serial port: a ring buffer instead of a VM exit
            # per byte, which also slowed journald enough to lose
            # _SYSTEMD_UNIT on short-lived senders.
            "-device",
            "virtio-serial-pci,id=journal_bus",
            "-chardev",
            f"file,id=journal,path={self.journal_file}",
            "-device",
            "virtconsole,chardev=journal,bus=journal_bus.0",
            "-pidfile",
            str(self.pid_file),
            "-qmp",
            f"unix:{self.qmp_socket},server=on,wait=off",
        ]

        if self.launch.foreground:
            # mon:stdio multiplexes the guest's first serial port with qemu's
            # HMP. Press Ctrl-A,c at the terminal to switch to HMP, Ctrl-A,c
            # again to return; Ctrl-A,x to quit qemu.
            cmd[cmd.index("-serial") + 1] = "mon:stdio"
        return cmd

    def _close_ssh_master(self) -> None:
        """Tear down the cell's ssh ControlMaster so no socket leaks across runs.

        `ssh -O exit` signals the master to close cleanly and unlink its
        socket; without it the master would linger ControlPersist=600s past the
        cell, and a same-port future cell could reuse a stale socket pointing at
        a dead guest. Best-effort -- no master may ever have opened, or
        ControlPersist may have expired it.
        """
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
        # Best-effort: ssh may be missing, or a wedged master may not answer.
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5
            )


def imagedir_for_host() -> Path:
    """Return the packer-image cache root, HOMELAB_CI_DIR from mise.toml.

    Created on first use, but never its parent: on Linux that parent is the
    scratch volume's mountpoint, so a host where the volume was never set up
    fails here. An unmounted volume whose empty mountpoint survives is not
    detected; the cache would then land on the root disk.
    """
    try:
        d = Path(os.environ["HOMELAB_CI_DIR"]).resolve()
    except KeyError:
        raise RuntimeError("HOMELAB_CI_DIR is unset; run the harness through mise") from None
    try:
        d.mkdir(exist_ok=True)
    except FileNotFoundError:
        raise RuntimeError(f"Imagedir parent {d.parent} does not exist; mount the qemu image volume") from None
    return d


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
