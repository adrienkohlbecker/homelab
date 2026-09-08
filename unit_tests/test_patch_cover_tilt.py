"""Unit tests for roles/z2m/files/patch_cover_tilt.py — Z2M tilt nullifier."""

import sys

import yaml
from conftest import load_repo_module

pct = load_repo_module("roles/z2m/files/patch_cover_tilt.py")


def test_missing_file_is_noop(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "devices.yaml"
    monkeypatch.setattr(sys, "argv", ["patch_cover_tilt.py", str(path)])

    assert pct.main() == 0
    assert capsys.readouterr().out == "OK\n"


def test_main_updates_matches_atomically_and_preserves_mode(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "devices.yaml"
    devices = {
        "0x1": {
            "friendly_name": "kitchen/left_shutter",
            "homeassistant": {"name": "Kitchen", "cover": {"existing": "field"}},
        },
        "0x2": {"friendly_name": "kitchen/right_shutter"},
        "0x3": {"friendly_name": "Bedroom Light"},
        "0x4": "not a device",
    }
    path.write_text(yaml.safe_dump(devices))
    path.chmod(0o640)
    monkeypatch.setattr(sys, "argv", ["patch_cover_tilt.py", str(path)])

    assert pct.main() == 0
    assert capsys.readouterr().out == "CHANGED\n"
    assert path.stat().st_mode & 0o777 == 0o640
    assert set(tmp_path.iterdir()) == {path}

    updated = yaml.safe_load(path.read_text())
    for device_id in ("0x1", "0x2"):
        cover = updated[device_id]["homeassistant"]["cover"]
        assert cover["tilt_status_topic"] is None
        assert cover["tilt_status_template"] is None
        assert cover["tilt_command_topic"] is None
    assert updated["0x1"]["homeassistant"]["name"] == "Kitchen"
    assert updated["0x1"]["homeassistant"]["cover"]["existing"] == "field"
    assert "homeassistant" not in updated["0x3"]
    assert updated["0x4"] == "not a device"

    assert pct.main() == 0
    assert capsys.readouterr().out == "OK\n"
