"""Unit tests for roles/wireguard/filter_plugins/wireguard_psk.py — PSK derivation."""

import base64
import importlib
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "roles" / "wireguard" / "filter_plugins" / "wireguard_psk.py"


def _load():
    spec = importlib.util.spec_from_file_location("wireguard_psk", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wg = _load()


class TestWireguardPsk:
    def test_returns_base64_string(self) -> None:
        result = wg.wireguard_psk("lab-phone", "myseed")
        decoded = base64.b64decode(result)
        assert len(decoded) == 32

    def test_deterministic(self) -> None:
        a = wg.wireguard_psk("lab-phone", "seed1")
        b = wg.wireguard_psk("lab-phone", "seed1")
        assert a == b

    def test_different_seeds_differ(self) -> None:
        a = wg.wireguard_psk("lab-phone", "seed1")
        b = wg.wireguard_psk("lab-phone", "seed2")
        assert a != b

    def test_different_pairs_differ(self) -> None:
        a = wg.wireguard_psk("lab-phone", "seed")
        b = wg.wireguard_psk("lab-pug", "seed")
        assert a != b

    def test_pair_order_matters(self) -> None:
        a = wg.wireguard_psk("lab-phone", "seed")
        b = wg.wireguard_psk("phone-lab", "seed")
        assert a != b

    def test_valid_wireguard_key_length(self) -> None:
        result = wg.wireguard_psk("a-b", "s")
        raw = base64.b64decode(result)
        assert len(raw) == 32


class TestFilterModule:
    def test_exposes_filter(self) -> None:
        fm = wg.FilterModule()
        filters = fm.filters()
        assert "wireguard_psk" in filters
        assert filters["wireguard_psk"] is wg.wireguard_psk
