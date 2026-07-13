"""Unit tests for roles/z2m/files/patch_cover_tilt.py — Z2M tilt nullifier."""

import importlib.util
import re
import sys
from pathlib import Path

import yaml

_MODULE_PATH = Path(__file__).resolve().parent.parent / "roles" / "z2m" / "files" / "patch_cover_tilt.py"


def _load():
    spec = importlib.util.spec_from_file_location("patch_cover_tilt", _MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pct = _load()


class TestPatch:
    def test_updates_only_matches_and_is_idempotent(self) -> None:
        devices = {
            "0x1": {
                "friendly_name": "Kitchen Blind A",
                "homeassistant": {"name": "Kitchen", "cover": {"existing": "field"}},
            },
            "0x2": {"friendly_name": "Kitchen Blind B"},
            "0x3": {"friendly_name": "Bedroom Light"},
            "0x4": "not a device",
        }

        assert pct.patch(devices, re.compile(r"Kitchen")) is True
        for device_id in ("0x1", "0x2"):
            cover = devices[device_id]["homeassistant"]["cover"]
            assert cover["tilt_status_topic"] is None
            assert cover["tilt_status_template"] is None
            assert cover["tilt_command_topic"] is None
        assert devices["0x1"]["homeassistant"]["name"] == "Kitchen"
        assert devices["0x1"]["homeassistant"]["cover"]["existing"] == "field"
        assert "homeassistant" not in devices["0x3"]
        assert devices["0x4"] == "not a device"
        assert pct.patch(devices, re.compile(r"Kitchen")) is False

    def test_missing_file_is_noop(self, tmp_path, monkeypatch, capsys) -> None:
        path = tmp_path / "devices.yaml"
        monkeypatch.setattr(sys, "argv", ["patch_cover_tilt.py", "shutter$", str(path)])

        assert pct.main() == 0
        assert capsys.readouterr().out == "OK\n"

    def test_main_updates_atomically_and_preserves_mode(self, tmp_path, monkeypatch, capsys) -> None:
        path = tmp_path / "devices.yaml"
        path.write_text('"0x1":\n  friendly_name: kitchen/shutter\n')
        path.chmod(0o640)
        monkeypatch.setattr(sys, "argv", ["patch_cover_tilt.py", "shutter$", str(path)])

        assert pct.main() == 0
        assert capsys.readouterr().out == "CHANGED\n"
        assert path.stat().st_mode & 0o777 == 0o640
        cover = yaml.safe_load(path.read_text())["0x1"]["homeassistant"]["cover"]
        assert set(cover) == {
            "tilt_status_topic",
            "tilt_status_template",
            "tilt_command_topic",
        }
        assert all(value is None for value in cover.values())

        assert pct.main() == 0
        assert capsys.readouterr().out == "OK\n"
