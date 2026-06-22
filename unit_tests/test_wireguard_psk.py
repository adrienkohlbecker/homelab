"""Unit tests for filter_plugins/wireguard_psk.py."""

import base64

import filter_plugins.wireguard_psk as wg


def test_derives_expected_hmac_key() -> None:
    result = wg.wireguard_psk("lab-phone", "myseed")
    assert result == "tdKX6ZCAE7lZYoreT1IAumcVTTcntOZLvLgtdtWACXI="
    assert len(base64.b64decode(result)) == 32


def test_deterministic() -> None:
    assert wg.wireguard_psk("lab-phone", "seed1") == wg.wireguard_psk("lab-phone", "seed1")


def test_seed_pair_and_pair_order_select_the_key() -> None:
    key = wg.wireguard_psk("lab-phone", "seed")
    assert key != wg.wireguard_psk("lab-phone", "other-seed")
    assert key != wg.wireguard_psk("lab-pug", "seed")
    assert key != wg.wireguard_psk("phone-lab", "seed")


def test_exposes_filter() -> None:
    filters = wg.FilterModule().filters()
    assert filters["wireguard_psk"] is wg.wireguard_psk
