"""Unit tests for roles/z2m/files/patch_cover_tilt.py — Z2M tilt nullifier."""

import importlib
import re
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "roles" / "z2m" / "files" / "patch_cover_tilt.py"


def _load():
    spec = importlib.util.spec_from_file_location("patch_cover_tilt", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pct = _load()


class TestPatch:
    def test_patches_matching_device(self) -> None:
        devices = {
            "0x1234": {"friendly_name": "Kitchen Blind", "homeassistant": {}},
        }
        changed = pct.patch(devices, re.compile(r"Kitchen"))
        assert changed is True
        cover = devices["0x1234"]["homeassistant"]["cover"]
        assert cover["tilt_status_topic"] is None
        assert cover["tilt_status_template"] is None
        assert cover["tilt_command_topic"] is None

    def test_skips_non_matching(self) -> None:
        devices = {
            "0x1234": {"friendly_name": "Bedroom Light"},
        }
        changed = pct.patch(devices, re.compile(r"Kitchen"))
        assert changed is False
        assert "homeassistant" not in devices["0x1234"]

    def test_creates_homeassistant_and_cover_keys(self) -> None:
        devices = {
            "0x1234": {"friendly_name": "Kitchen Blind"},
        }
        pct.patch(devices, re.compile(r"Kitchen"))
        assert "homeassistant" in devices["0x1234"]
        assert "cover" in devices["0x1234"]["homeassistant"]

    def test_preserves_existing_homeassistant_keys(self) -> None:
        devices = {
            "0x1234": {
                "friendly_name": "Kitchen Blind",
                "homeassistant": {"some_other": "value", "cover": {"existing": "field"}},
            },
        }
        pct.patch(devices, re.compile(r"Kitchen"))
        ha = devices["0x1234"]["homeassistant"]
        assert ha["some_other"] == "value"
        assert ha["cover"]["existing"] == "field"
        assert ha["cover"]["tilt_status_topic"] is None

    def test_idempotent(self) -> None:
        devices = {
            "0x1234": {"friendly_name": "Kitchen Blind"},
        }
        pct.patch(devices, re.compile(r"Kitchen"))
        changed = pct.patch(devices, re.compile(r"Kitchen"))
        assert changed is False

    def test_non_dict_values_skipped(self) -> None:
        devices = {"0x1234": "not a dict"}
        changed = pct.patch(devices, re.compile(r".*"))
        assert changed is False

    def test_multiple_devices(self) -> None:
        devices = {
            "0x1": {"friendly_name": "Kitchen Blind A"},
            "0x2": {"friendly_name": "Kitchen Blind B"},
            "0x3": {"friendly_name": "Bedroom Light"},
        }
        changed = pct.patch(devices, re.compile(r"Kitchen"))
        assert changed is True
        assert "cover" in devices["0x1"].get("homeassistant", {})
        assert "cover" in devices["0x2"].get("homeassistant", {})
        assert "homeassistant" not in devices["0x3"]

    def test_regex_pattern(self) -> None:
        devices = {
            "0x1": {"friendly_name": "Volet Cuisine"},
            "0x2": {"friendly_name": "Volet Salon"},
        }
        changed = pct.patch(devices, re.compile(r"^Volet"))
        assert changed is True
        assert "cover" in devices["0x1"]["homeassistant"]
        assert "cover" in devices["0x2"]["homeassistant"]
