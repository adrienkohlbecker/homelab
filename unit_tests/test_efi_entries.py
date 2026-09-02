"""Unit tests for roles/boot/files/efi_entries.py — EFI NVRAM converger.

Drives the converge decision logic in --check mode against synthetic
`efibootmgr -v` / `mdadm --detail --export` / `lsblk` / `findmnt` output, so
the matching, removal, per-disk expansion and BootOrder logic is exercised
without touching real firmware NVRAM.
"""

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "roles" / "boot" / "files" / "efi_entries.py"


def _load():
    spec = importlib.util.spec_from_file_location("efi_entries", _MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


efi = _load()


# --- Desired entry lists (mirror roles/boot/defaults/main.yml) ------------

LOADERS = [
    {"label": "rEFInd", "loader": "\\EFI\\refind\\refind_x64.efi", "multi_disk": True},
    {"label": "ZFSBootMenu", "loader": "\\EFI\\ZBM\\VMLINUZ.EFI"},
    {"label": "ZFSBootMenu (Backup)", "loader": "\\EFI\\ZBM\\VMLINUZ-BACKUP.EFI"},
]

# --- Synthetic command output --------------------------------------------


def _efibootmgr_v(entries, timeout=3, current="0000"):
    """Render a synthetic `efibootmgr -v` dump.

    entries: list of (num, label, devpath) tuples.
    """
    lines = [f"BootCurrent: {current}", f"Timeout: {timeout} seconds"]
    lines.append("BootOrder: " + ",".join(entry[0] for entry in entries))
    for num, label, devpath in entries:
        lines.append(f"Boot{num}* {label}\t{devpath}")
    return "\n".join(lines)


def _hd(uuid, loader):
    return f"HD(1,GPT,{uuid},0x800,0x100000)/File({loader})"


def _make_run(efibootmgr_out, mdadm_export, lsblk_map, findmnt):
    def _run(cmd):
        if cmd[0] == "efibootmgr":
            return efibootmgr_out
        if cmd[0] == "mdadm":
            return mdadm_export
        if cmd[0] == "findmnt":
            return findmnt
        if cmd[0] == "lsblk":
            return json.dumps(lsblk_map[cmd[-1]])
        raise AssertionError(f"unexpected command {cmd}")

    return _run


def run_check(
    monkeypatch,
    capsys,
    desired,
    efibootmgr_out,
    *,
    mdadm_export=None,
    lsblk_map=None,
    findmnt=None,
    timeout="3",
):
    """Run efi_entries.main() in --check mode against synthetic state; return the JSON result."""
    monkeypatch.setattr(efi, "run", _make_run(efibootmgr_out, mdadm_export, lsblk_map, findmnt))
    monkeypatch.setattr(efi.os.path, "exists", lambda p: mdadm_export is not None and p == "/dev/md/efi")
    monkeypatch.setenv("BOOT_EFI_ENTRIES", json.dumps(desired))
    monkeypatch.setenv("BOOT_EFI_TIMEOUT", timeout)
    monkeypatch.setattr(sys, "argv", ["efi_entries.py", "--check"])
    efi.main()
    return json.loads(capsys.readouterr().out)


# --- Single-disk (box) fixtures ------------------------------------------

_SINGLE: dict[str, Any] = dict(
    findmnt="/dev/sda1",
    lsblk_map={"/dev/sda1": {"blockdevices": [{"name": "sda1", "pkname": "sda", "partn": 1, "partuuid": "AAA-0"}]}},
)


class TestSingleDisk:
    def test_fresh_creates_all_loaders(self, monkeypatch, capsys):
        out = run_check(
            monkeypatch,
            capsys,
            LOADERS,
            _efibootmgr_v([("0005", "UEFI OS", _hd("fff", "\\EFI\\BOOT\\BOOTX64.EFI"))]),
            **_SINGLE,
        )
        assert out["changed"]
        for label in ("rEFInd", "ZFSBootMenu", "ZFSBootMenu (Backup)"):
            assert any(f"create '{label}'" in a for a in out["actions"]), out["actions"]
        # Single disk → no per-disk suffix.
        assert not any("(disk" in a for a in out["actions"])
        # The removable-media fallback is never pruned.
        assert not any("UEFI OS" in a for a in out["actions"])

    def test_converged_is_idempotent(self, monkeypatch, capsys):
        converged = _efibootmgr_v(
            [
                ("0000", "rEFInd", _hd("AAA-0", "\\EFI\\refind\\refind_x64.efi")),
                ("0001", "ZFSBootMenu", _hd("AAA-0", "\\EFI\\ZBM\\VMLINUZ.EFI")),
                ("0002", "ZFSBootMenu (Backup)", _hd("AAA-0", "\\EFI\\ZBM\\VMLINUZ-BACKUP.EFI")),
            ],
        )
        out = run_check(monkeypatch, capsys, LOADERS, converged, **_SINGLE)
        assert not out["changed"], out["actions"]


# --- Multi-disk mirror (lab) fixtures ------------------------------------


def _mdadm_export(roles):
    lines = ["MD_LEVEL=raid1", "MD_DEVICES=3"]
    for i in roles:
        lines.append(f"MD_DEVICE_dev_nvme{i}n1p1_ROLE={i}")
        lines.append(f"MD_DEVICE_dev_nvme{i}n1p1_DEV=/dev/nvme{i}n1p1")
    return "\n".join(lines)


# Mirror members carry their md holder as a second blockdevice; the name filter
# in _add_disk must pick the partition, not the holder.
_LSBLK3 = {
    f"/dev/nvme{i}n1p1": {
        "blockdevices": [
            {"name": f"nvme{i}n1p1", "pkname": f"nvme{i}n1", "partn": 1, "partuuid": f"UU-{i}"},
            {"name": "md127", "pkname": None, "partn": None, "partuuid": None},
        ]
    }
    for i in range(3)
}


class TestMultiDisk:
    def test_fresh_mirror_expands_per_disk(self, monkeypatch, capsys):
        out = run_check(
            monkeypatch,
            capsys,
            LOADERS,
            _efibootmgr_v([("0009", "UEFI OS", _hd("zzz", "\\EFI\\BOOT\\BOOTX64.EFI"))]),
            mdadm_export=_mdadm_export(range(3)),
            lsblk_map=_LSBLK3,
        )
        for i in range(3):
            assert any(f"create 'rEFInd (disk {i})'" in a for a in out["actions"]), out["actions"]
        # Non-multi_disk loaders stay single even on a mirror.
        assert any("create 'ZFSBootMenu'" in a for a in out["actions"])
        assert not any("ZFSBootMenu (disk" in a for a in out["actions"])

    def test_degraded_keeps_absent_member_entry(self, monkeypatch, capsys):
        # disk 1 has failed out of the mirror; its NVRAM entry must survive, and
        # the surviving disks keep their stable slot indices (0 and 2).
        existing = _efibootmgr_v(
            [
                ("0000", "rEFInd (disk 0)", _hd("UU-0", "\\EFI\\refind\\refind_x64.efi")),
                ("0001", "rEFInd (disk 1)", _hd("UU-1", "\\EFI\\refind\\refind_x64.efi")),
                ("0002", "rEFInd (disk 2)", _hd("UU-2", "\\EFI\\refind\\refind_x64.efi")),
                ("0003", "ZFSBootMenu", _hd("UU-0", "\\EFI\\ZBM\\VMLINUZ.EFI")),
                ("0004", "ZFSBootMenu (Backup)", _hd("UU-0", "\\EFI\\ZBM\\VMLINUZ-BACKUP.EFI")),
            ],
        )
        out = run_check(
            monkeypatch,
            capsys,
            LOADERS,
            existing,
            mdadm_export=_mdadm_export([0, 2]),
            lsblk_map=_LSBLK3,
        )
        # The disk-1 entry is neither removed nor recreated under a shifted index.
        assert not any("rEFInd (disk 1)" in a for a in out["actions"]), out["actions"]
        assert not any("create 'rEFInd (disk 0)'" in a or "create 'rEFInd (disk 2)'" in a for a in out["actions"])
        assert not any(a.startswith(("remove", "create")) for a in out["actions"]), out["actions"]


# --- Stale duplicate vs orphan protection (findings #1, #2) ---------------


class TestStaleRemoval:
    def test_firmware_dup_removed_fallback_kept(self, monkeypatch, capsys):
        existing = _efibootmgr_v(
            [
                ("0000", "rEFInd", _hd("AAA-0", "\\EFI\\refind\\refind_x64.efi")),
                ("0001", "ZFSBootMenu", _hd("AAA-0", "\\EFI\\ZBM\\VMLINUZ.EFI")),
                ("0002", "ZFSBootMenu (Backup)", _hd("AAA-0", "\\EFI\\ZBM\\VMLINUZ-BACKUP.EFI")),
                ("0007", "rEFInd Boot Manager", _hd("AAA-0", "\\EFI\\refind\\refind_x64.efi")),  # firmware dup
                ("0008", "UEFI OS", _hd("AAA-0", "\\EFI\\BOOT\\BOOTX64.EFI")),  # removable fallback
            ],
        )
        out = run_check(monkeypatch, capsys, LOADERS, existing, **_SINGLE)
        assert any("rEFInd Boot Manager" in a and "remove" in a for a in out["actions"]), out["actions"]
        assert not any("UEFI OS" in a for a in out["actions"]), out["actions"]

    def test_lone_loader_pointer_not_orphaned(self, monkeypatch, capsys):
        # The only entry pointing at the refind binary is a firmware one and no
        # managed entry matched that loader — keep it, never orphan the chain.
        existing = _efibootmgr_v([("0007", "rEFInd Boot Manager", _hd("AAA-0", "\\EFI\\refind\\refind_x64.efi"))])
        out = run_check(monkeypatch, capsys, LOADERS, existing, **_SINGLE)
        assert not any("rEFInd Boot Manager" in a and "remove" in a for a in out["actions"]), out["actions"]


# --- Input validation (finding #3) ----------------------------------------


class TestValidation:
    @pytest.mark.parametrize("key", ["label", "loader"])
    def test_leading_dash_rejected(self, key):
        entry = {"label": "X", "loader": "\\EFI\\x.efi"}
        entry[key] = "-evil"
        with pytest.raises(ValueError, match="must not start with '-'"):
            efi._validate_desired([entry])

    def test_control_char_rejected(self):
        with pytest.raises(ValueError, match="control character"):
            efi._validate_desired([{"label": "bad\tlabel", "loader": "\\EFI\\x.efi"}])

    def test_duplicate_expanded_labels_rejected(self, monkeypatch, capsys):
        dupe = [
            {"label": "rEFInd", "loader": "\\EFI\\a.efi"},
            {"label": "rEFInd", "loader": "\\EFI\\b.efi"},
        ]
        with pytest.raises(ValueError, match="duplicate EFI entry labels"):
            run_check(monkeypatch, capsys, dupe, _efibootmgr_v([]), **_SINGLE)


# --- Pure helpers ---------------------------------------------------------


class TestHelpers:
    def test_loader_eq_normalizes_slashes_and_case(self):
        assert efi.loader_eq("/EFI/refind/REFIND_X64.EFI", "\\EFI\\refind\\refind_x64.efi")
        assert not efi.loader_eq("", "\\EFI\\x.efi")

    def test_removable_fallback_detection(self):
        assert efi._is_removable_fallback("\\EFI\\BOOT\\BOOTX64.EFI")
        assert efi._is_removable_fallback("\\EFI\\BOOT\\BOOTAA64.EFI")
        assert not efi._is_removable_fallback("\\EFI\\refind\\refind_x64.efi")


class TestParseEfibootmgr:
    def test_parse_v18_detail_lines(self, monkeypatch):
        uuid = "6191bc58-2e95-4493-b10c-b83df5181e9f"
        out = "\n".join(
            [
                "BootCurrent: 0001",
                "Timeout: 3 seconds",
                "BootOrder: 0001,0002",
                "Boot0001* rEFInd\t" + _hd(uuid, "\\EFI\\refind\\refind_x64.efi"),
                "      dp: 04 01 2a 00 02 00 00 00 / 7f ff 04 00",
                "      data: 00 42 4f",
                "Boot0002* ZFSBootMenu\t" + _hd(uuid, "\\EFI\\ZBM\\VMLINUZ.EFI"),
                "      dp: 04 01 2a 00 02 00 00 00 / 7f ff 04 00",
            ]
        )
        monkeypatch.setattr(efi, "run", lambda cmd: out)
        entries, order, timeout = efi.parse_efibootmgr()
        assert [entry["label"] for entry in entries] == ["rEFInd", "ZFSBootMenu"]
        assert order == ["0001", "0002"]
        assert timeout == 3
