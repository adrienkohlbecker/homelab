"""Unit tests for machine.py functions not covered by existing test_*.py files."""

import asyncio
import fcntl
import os
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import machine
import matrix
import pytest

# ---------------------------------------------------------------------------
# qemu_user_net_args
# ---------------------------------------------------------------------------


class TestQemuUserNetArgs:
    def test_returns_empty_for_unknown_machine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            machine, "_load_test_topology", lambda: {"hosts": {}, "partitions": {"physical": {"cidr": "10.234.0.0/16"}}}
        )
        assert machine.qemu_user_net_args("nonexistent") == ""

    def test_returns_string_for_known_machine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        topo = {
            "hosts": {"lab": {"physical": "10.234.0.2"}},
            "partitions": {"physical": {"cidr": "10.234.0.0/16"}},
        }
        monkeypatch.setattr(machine, "_load_test_topology", lambda: topo)
        result = machine.qemu_user_net_args("lab")
        assert result.startswith(",")
        assert "net=10.234.0.0/16" in result
        assert "dhcpstart=10.234.0.2" in result


# ---------------------------------------------------------------------------
# _workdir_is_orphan
# ---------------------------------------------------------------------------


class TestWorkdirIsOrphan:
    def test_orphan_when_no_live_file(self, tmp_path: Path) -> None:
        workdir = tmp_path / "tmp_test"
        workdir.mkdir()
        assert machine._workdir_is_orphan(workdir) is True

    def test_orphan_when_live_unlocked(self, tmp_path: Path) -> None:
        workdir = tmp_path / "tmp_test"
        workdir.mkdir()
        (workdir / ".live").write_text("")
        assert machine._workdir_is_orphan(workdir) is True

    def test_not_orphan_when_live_locked(self, tmp_path: Path) -> None:
        workdir = tmp_path / "tmp_test"
        workdir.mkdir()
        live = workdir / ".live"
        live.write_text("")
        fd = os.open(str(live), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            assert machine._workdir_is_orphan(workdir) is False
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# sweep_stale_workdirs
# ---------------------------------------------------------------------------


class TestSweepStaleWorkdirs:
    def test_reaps_old_unlocked_tmpdir(self, tmp_path: Path) -> None:
        workdir = tmp_path / "tmp_stale"
        workdir.mkdir()
        (workdir / ".live").write_text("")
        old_time = time.time() - 120
        os.utime(str(workdir), (old_time, old_time))
        machine.sweep_stale_workdirs(tmp_path)
        assert not workdir.exists()

    def test_keeps_recent_tmpdir(self, tmp_path: Path) -> None:
        workdir = tmp_path / "tmp_recent"
        workdir.mkdir()
        (workdir / ".live").write_text("")
        machine.sweep_stale_workdirs(tmp_path)
        assert workdir.exists()

    def test_keeps_locked_tmpdir(self, tmp_path: Path) -> None:
        workdir = tmp_path / "tmp_locked"
        workdir.mkdir()
        live = workdir / ".live"
        live.write_text("")
        fd = os.open(str(live), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            old_time = time.time() - 120
            os.utime(str(workdir), (old_time, old_time))
            machine.sweep_stale_workdirs(tmp_path)
            assert workdir.exists()
        finally:
            os.close(fd)

    def test_ignores_nonexistent_dir(self) -> None:
        machine.sweep_stale_workdirs(Path("/nonexistent/path"))

    def test_ignores_non_tmp_dirs(self, tmp_path: Path) -> None:
        other = tmp_path / "regular_dir"
        other.mkdir()
        old_time = time.time() - 120
        os.utime(str(other), (old_time, old_time))
        machine.sweep_stale_workdirs(tmp_path)
        assert other.exists()

    def test_ignores_packer_build_dirs(self, tmp_path: Path) -> None:
        workdir = tmp_path / ".build-stale"
        workdir.mkdir()
        old_time = time.time() - 120
        os.utime(str(workdir), (old_time, old_time))

        machine.sweep_stale_workdirs(tmp_path)

        assert workdir.exists()


def test_failure_artifact_collection_continues_after_capture_error(
    machine_factory: Callable[..., machine.Machine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = machine_factory()
    labels: list[str] = []

    async def collect(label: str, _dest: Path, *_command: str) -> bool:
        labels.append(label)
        if label == "Kernel ring buffer":
            raise OSError("guest disappeared")
        return True

    monkeypatch.setattr(instance, "_collect_remote_to_file", collect)

    asyncio.run(instance.collect_failure_artifacts())

    assert labels == ["Kernel ring buffer", "Failed units"]


# ---------------------------------------------------------------------------
# UBUNTU_RELEASES / QemuMachineSpec constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_supported_ubuntu_releases(self) -> None:
        assert set(matrix.UBUNTU_RELEASES) == {"noble", "resolute"}
        assert matrix.UBUNTU_RELEASES["noble"] == "24.04"

    def test_default_ubuntu_is_noble(self) -> None:
        assert matrix.DEFAULT_UBUNTU == "noble"

    def test_machine_choices_tuple(self) -> None:
        assert isinstance(machine.MACHINE_CHOICES, tuple)
        assert "minimal" in machine.MACHINE_CHOICES
        assert "lab" in machine.MACHINE_CHOICES
        assert "pug" in machine.MACHINE_CHOICES

    def test_qemu_specs_match_choices(self) -> None:
        assert set(machine.QEMU_MACHINE_SPECS.keys()) == set(machine.MACHINE_CHOICES)

    def test_only_minimal_uses_a_cloud_image(self) -> None:
        assert machine.QEMU_MACHINE_SPECS["minimal"].cloud_image is True
        assert machine.QEMU_MACHINE_SPECS["lab"].cloud_image is False
        assert machine.QEMU_MACHINE_SPECS["pug"].cloud_image is False

    def test_each_packer_machine_uses_its_inventory_host(self) -> None:
        assert machine.QEMU_MACHINE_SPECS["lab"].inventory_host == "lab"
        assert machine.QEMU_MACHINE_SPECS["pug"].inventory_host == "pug"


class TestDiscoverPackerDisks:
    def test_returns_contiguous_disks_and_format(self, tmp_path: Path) -> None:
        second = tmp_path / "packer-ubuntu-2.raw"
        first = tmp_path / "packer-ubuntu-1.raw"
        second.touch()
        first.touch()

        paths, disk_format = machine.discover_packer_disks(tmp_path)

        assert paths == [first, second]
        assert disk_format == "raw"

    def test_rejects_missing_disk_index(self, tmp_path: Path) -> None:
        (tmp_path / "packer-ubuntu-1.qcow2").touch()
        (tmp_path / "packer-ubuntu-3.qcow2").touch()

        with pytest.raises(RuntimeError, match=r"indexes.*\[1, 3\].*\[1, 2\]"):
            machine.discover_packer_disks(tmp_path)

    def test_rejects_mixed_formats(self, tmp_path: Path) -> None:
        (tmp_path / "packer-ubuntu-1.raw").touch()
        (tmp_path / "packer-ubuntu-2.qcow2").touch()

        with pytest.raises(RuntimeError, match="mix formats"):
            machine.discover_packer_disks(tmp_path)

    def test_rejects_empty_directory(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="no Packer disks"):
            machine.discover_packer_disks(tmp_path)


class TestUefiDrives:
    def test_copies_required_vars_template(
        self,
        machine_factory: Callable[..., machine.Machine],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        code = tmp_path / "code.fd"
        variables = tmp_path / "vars.fd"
        code.write_bytes(b"code")
        variables.write_bytes(b"variables")
        monkeypatch.setattr(machine, "uefi_firmware_paths_for", lambda _arch: (code, variables))
        instance = machine_factory(host_arch="aarch64")

        drives = asyncio.run(instance._uefi_drives())

        copied_vars = instance.workdir_path / "uefi-vars.fd"
        assert copied_vars.read_bytes() == b"variables"
        assert drives == [
            f"file={code},if=pflash,unit=0,format=raw,readonly=on",
            f"file={copied_vars},if=pflash,unit=1,format=raw",
        ]


# ---------------------------------------------------------------------------
# Machine.__init__ ubuntu validation
# ---------------------------------------------------------------------------


class TestMachineUbuntuValidation:
    def test_unknown_ubuntu_raises(self, machine_factory: Callable[..., machine.Machine]) -> None:
        with pytest.raises(ValueError, match="Unknown Ubuntu release"):
            machine_factory(machine="lab", role="test", ubuntu_name="bogus")


class TestMachineArtifactOwnership:
    def test_clears_and_cleans_every_per_run_artifact(
        self,
        machine_factory: Callable[..., machine.Machine],
        tmp_path: Path,
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        artifacts = [
            out / f"lab.noble.testrole.{suffix}.ansi"
            for suffix in ("output", "journal", "boot", "dmesg", "systemctl-failed", "passt")
        ]
        for artifact in artifacts:
            artifact.write_text("stale")

        instance = machine_factory()
        assert all(not artifact.exists() for artifact in artifacts)

        for artifact in artifacts:
            artifact.write_text("current")
        instance.cleanup_logs()
        assert all(not artifact.exists() for artifact in artifacts)


class TestSystemReadiness:
    def test_accepts_running_state(
        self,
        machine_factory: Callable[..., machine.Machine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        instance = machine_factory()

        async def ssh_command(*args: str, check: bool = True) -> SimpleNamespace:
            assert args == ("systemctl", "is-system-running", "--wait")
            assert check is False
            return SimpleNamespace(exitcode=0, stdout=["running"])

        monkeypatch.setattr(instance, "ssh_command", ssh_command)
        asyncio.run(instance.ensure_system_running())

    def test_reports_failed_units(
        self,
        machine_factory: Callable[..., machine.Machine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        instance = machine_factory()
        responses = iter(
            (
                SimpleNamespace(exitcode=1, stdout=["degraded"]),
                SimpleNamespace(exitcode=0, stdout=["broken.service loaded failed failed"]),
            )
        )

        async def ssh_command(*args: str, check: bool = True) -> SimpleNamespace:
            assert check is False
            return next(responses)

        monkeypatch.setattr(instance, "ssh_command", ssh_command)
        with pytest.raises(RuntimeError, match=r"(?s)degraded.*broken\.service"):
            asyncio.run(instance.ensure_system_running())


class TestAnsibleControllerStaging:
    def test_stages_once_on_first_ansible_command(
        self,
        machine_factory: Callable[..., machine.Machine],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for directory in (
            "group_vars",
            "host_vars",
            "roles",
            "data",
            "test/playbooks",
        ):
            (tmp_path / directory).mkdir(parents=True)
        (tmp_path / "test/playbooks/site.yml").write_text("fixture site\n")
        (tmp_path / "test/playbooks/_environment.yml").write_text("environment\n")

        staged_calls = 0

        def ensure_mitogen() -> None:
            nonlocal staged_calls
            staged_calls += 1

        async def run_command(*args: object, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(exitcode=0, stdout=[])

        monkeypatch.setattr(machine, "ensure_mitogen_symlink", ensure_mitogen)
        monkeypatch.setattr(machine, "run_command", run_command)

        m = machine_factory()
        (m.workdir_path / "site.yml").write_text("production site\n")
        assert not (m.workdir_path / "roles").exists()

        asyncio.run(m.ansible_command(str(m.workdir_path / "site.yml")))
        asyncio.run(m.ansible_command(str(m.workdir_path / "_environment.yml")))

        assert staged_calls == 1
        assert (m.workdir_path / "roles").is_dir()
        assert (m.workdir_path / "_environment.yml").read_text() == "environment\n"
        assert (m.workdir_path / "site.yml").read_text() == "production site\n"


def test_ensure_booted_reports_early_qemu_exit(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(machine="lab", role="test")
    m.proc = cast(asyncio.subprocess.Process, SimpleNamespace(returncode=1))

    with pytest.raises(RuntimeError, match=r"qemu wrapper exited with 1.*lab\.noble\.test\.boot\.ansi"):
        asyncio.run(m.ensure_booted())


# ---------------------------------------------------------------------------
# _cell_loopback_host: per-cell loopback so concurrent qemu hostfwds don't
# collide on the shared ephemeral port range.
# ---------------------------------------------------------------------------


class TestCellLoopbackHost:
    def test_linux_derives_per_pid_address_in_127_8(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(machine.platform, "system", lambda: "Linux")
        # pid 0x010203 -> 127.1.2.3 (each octet a byte of the 24-bit pid).
        monkeypatch.setattr(machine.os, "getpid", lambda: 0x010203)
        assert machine._cell_loopback_host() == "127.1.2.3"

    def test_linux_masks_pid_into_24_bits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A pid above 2^24 wraps into the low 24 bits rather than overflowing
        # the address; the high byte stays 127.
        monkeypatch.setattr(machine.platform, "system", lambda: "Linux")
        monkeypatch.setattr(machine.os, "getpid", lambda: 0xAB010203)
        assert machine._cell_loopback_host() == "127.1.2.3"

    def test_non_linux_keeps_single_loopback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Only 127.0.0.1 is configured on macOS by default.
        monkeypatch.setattr(machine.platform, "system", lambda: "Darwin")
        assert machine._cell_loopback_host() == machine.SSH_HOST

    def test_explicit_loopback_threads_through_ssh_and_ansible(
        self, machine_factory: Callable[..., machine.Machine]
    ) -> None:
        # A pinned per-cell address must reach every controller-side endpoint:
        # the SSH target, the ControlMaster socket path (host-keyed so two cells
        # reusing a port don't share one socket), and ansible's connection vars.
        m = machine_factory(ssh_port=2222, ssh_user="vagrant", loopback_host="127.5.6.7")
        assert m.format_ssh_cmd()[-1] == "vagrant@127.5.6.7"
        assert m.ssh_control_path == "/tmp/homelab-cm-127.5.6.7-2222"
        cmd = m.format_ansible_cmd("site.yml")
        assert "ansible_ssh_host=127.5.6.7" in cmd
        assert "wan_probe_host=127.5.6.7" in cmd

    def test_explicit_loopback_binds_hostfwds(self, machine_factory: Callable[..., machine.Machine]) -> None:
        m = machine_factory(machine="lab", loopback_host="127.5.6.7")
        m.ssh_port = 2222
        m.wan_forward_ports = {"tcp": {}, "udp": {}}
        m._net_backend = "slirp"
        netdev, _ = m._netdev_args()
        assert "hostfwd=tcp:127.5.6.7:2222-:22" in netdev
