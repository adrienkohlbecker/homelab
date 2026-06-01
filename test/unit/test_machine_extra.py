"""Unit tests for machine.py functions not covered by existing test_*.py files.

Covers: resolve_default_machine, qemu_user_net_args, passt_address_args,
_qemu_ansible_args, _workdir_is_orphan, sweep_stale_workdirs, _read_vm_hwm,
UBUNTU_RELEASES, QemuMachineSpec.
"""

import fcntl
import os
import time
from pathlib import Path

import pytest

import machine


# ---------------------------------------------------------------------------
# resolve_default_machine
# ---------------------------------------------------------------------------


class TestResolveDefaultMachine:
    def test_returns_box_when_no_meta(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        assert machine.resolve_default_machine("nonexistent_role") == "box"

    def test_reads_machine_from_meta(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        meta = tmp_path / "roles" / "myrole" / "meta" / "test.yml"
        meta.parent.mkdir(parents=True)
        meta.write_text("machine: box_deps\n")
        assert machine.resolve_default_machine("myrole") == "box_deps"

    def test_defaults_to_box_when_no_machine_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        meta = tmp_path / "roles" / "myrole" / "meta" / "test.yml"
        meta.parent.mkdir(parents=True)
        meta.write_text("ubuntu: [noble]\n")
        assert machine.resolve_default_machine("myrole") == "box"

    def test_exits_on_unknown_machine(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        meta = tmp_path / "roles" / "myrole" / "meta" / "test.yml"
        meta.parent.mkdir(parents=True)
        meta.write_text("machine: bogus\n")
        with pytest.raises(SystemExit, match="bogus"):
            machine.resolve_default_machine("myrole")

    def test_exits_on_parse_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        meta = tmp_path / "roles" / "myrole" / "meta" / "test.yml"
        meta.parent.mkdir(parents=True)
        meta.write_text(": : :\n  - [\n")
        with pytest.raises(SystemExit, match="parse error"):
            machine.resolve_default_machine("myrole")


# ---------------------------------------------------------------------------
# qemu_user_net_args
# ---------------------------------------------------------------------------


class TestQemuUserNetArgs:
    def test_returns_empty_for_unknown_machine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(machine, "_load_test_topology", lambda: {"hosts": {}, "partitions": {"physical": {"cidr": "10.234.0.0/16"}}})
        assert machine.qemu_user_net_args("nonexistent") == ""

    def test_returns_string_for_known_machine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        topo = {
            "hosts": {"box": {"physical": "10.234.0.2"}},
            "partitions": {"physical": {"cidr": "10.234.0.0/16"}},
        }
        monkeypatch.setattr(machine, "_load_test_topology", lambda: topo)
        result = machine.qemu_user_net_args("box")
        assert result.startswith(",")
        assert "net=10.234.0.0/16" in result
        assert "dhcpstart=10.234.0.2" in result


# ---------------------------------------------------------------------------
# passt_address_args
# ---------------------------------------------------------------------------


class TestPasstAddressArgs:
    def test_returns_empty_for_unknown_machine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(machine, "_load_test_topology", lambda: {"hosts": {}, "partitions": {"physical": {"cidr": "10.234.0.0/16"}}})
        assert machine.passt_address_args("nonexistent") == []

    def test_returns_flags_for_known_machine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        topo = {
            "hosts": {"box": {"physical": "10.234.0.2"}},
            "partitions": {"physical": {"cidr": "10.234.0.0/16"}},
        }
        monkeypatch.setattr(machine, "_load_test_topology", lambda: topo)
        result = machine.passt_address_args("box")
        assert "--address" in result
        assert "10.234.0.2" in result
        assert "--netmask" in result
        assert "--gateway" in result


# ---------------------------------------------------------------------------
# _qemu_ansible_args
# ---------------------------------------------------------------------------


class TestQemuAnsibleArgs:
    def test_no_overlay_for_box(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        spec = machine.QemuMachineSpec(ssh_user="vagrant", inventory_host="box", packer_image="box")
        assert machine._qemu_ansible_args(spec) == []

    def test_overlay_for_lab(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        overlay = tmp_path / "host_vars" / "lab-qemu.yml"
        overlay.parent.mkdir(parents=True)
        overlay.write_text("qemu_test: true\n")
        spec = machine.QemuMachineSpec(ssh_user="vagrant", inventory_host="lab", packer_image="lab", os_disk_count=9)
        result = machine._qemu_ansible_args(spec)
        assert len(result) == 2
        assert result[0] == "-e"
        assert "lab-qemu.yml" in result[1]


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


# ---------------------------------------------------------------------------
# _read_vm_hwm
# ---------------------------------------------------------------------------


class TestReadVmHwm:
    def test_returns_zero_for_nonexistent_pid(self) -> None:
        assert machine._read_vm_hwm(999999999) == 0

    def test_returns_positive_for_self(self) -> None:
        pid = os.getpid()
        result = machine._read_vm_hwm(pid)
        if Path(f"/proc/{pid}/status").exists():
            assert result > 0
        else:
            assert result == 0


# ---------------------------------------------------------------------------
# UBUNTU_RELEASES / QemuMachineSpec constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_ubuntu_releases_has_jammy(self) -> None:
        assert "jammy" in machine.UBUNTU_RELEASES
        assert machine.UBUNTU_RELEASES["jammy"] == "22.04"

    def test_ubuntu_releases_has_noble(self) -> None:
        assert "noble" in machine.UBUNTU_RELEASES
        assert machine.UBUNTU_RELEASES["noble"] == "24.04"

    def test_default_ubuntu_is_jammy(self) -> None:
        assert machine.DEFAULT_UBUNTU == "jammy"

    def test_machine_choices_tuple(self) -> None:
        assert isinstance(machine.MACHINE_CHOICES, tuple)
        assert "box" in machine.MACHINE_CHOICES
        assert "minimal" in machine.MACHINE_CHOICES

    def test_qemu_specs_match_choices(self) -> None:
        assert set(machine.QEMU_MACHINE_SPECS.keys()) == set(machine.MACHINE_CHOICES)

    def test_minimal_has_no_packer_image(self) -> None:
        assert machine.QEMU_MACHINE_SPECS["minimal"].packer_image is None

    def test_box_has_packer_image(self) -> None:
        assert machine.QEMU_MACHINE_SPECS["box"].packer_image == "box"

    def test_box_deps_shares_inventory_host_with_box(self) -> None:
        assert machine.QEMU_MACHINE_SPECS["box_deps"].inventory_host == "box"


# ---------------------------------------------------------------------------
# Machine.__post_init__ ubuntu validation
# ---------------------------------------------------------------------------


class TestMachineUbuntuValidation:
    def test_unknown_ubuntu_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(machine, "OUT_DIR", tmp_path / "out")
        with pytest.raises(ValueError, match="Unknown Ubuntu release"):
            machine.Machine(
                ssh_port=2222,
                ssh_user="vagrant",
                ansible_args=[],
                inventory_host="box",
                machine="box",
                role="test",
                keep_vm=False,
                ubuntu_name="bogus",
                machine_timeout=300,
            )
