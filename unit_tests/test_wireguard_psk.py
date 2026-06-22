"""Unit tests for filter_plugins/wireguard_psk.py."""

import base64
import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "filter_plugins" / "wireguard_psk.py"


def _load():
    spec = importlib.util.spec_from_file_location("wireguard_psk", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wg = _load()


class TestWireguardPsk:
    def test_derives_expected_hmac_key(self) -> None:
        result = wg.wireguard_psk("lab-phone", "myseed")
        assert result == "tdKX6ZCAE7lZYoreT1IAumcVTTcntOZLvLgtdtWACXI="
        assert len(base64.b64decode(result)) == 32

    def test_deterministic(self) -> None:
        a = wg.wireguard_psk("lab-phone", "seed1")
        b = wg.wireguard_psk("lab-phone", "seed1")
        assert a == b

    def test_seed_pair_and_pair_order_select_the_key(self) -> None:
        key = wg.wireguard_psk("lab-phone", "seed")
        assert key != wg.wireguard_psk("lab-phone", "other-seed")
        assert key != wg.wireguard_psk("lab-pug", "seed")
        assert key != wg.wireguard_psk("phone-lab", "seed")


class TestFilterModule:
    def test_exposes_filter(self) -> None:
        fm = wg.FilterModule()
        filters = fm.filters()
        assert "wireguard_psk" in filters
        assert filters["wireguard_psk"] is wg.wireguard_psk
