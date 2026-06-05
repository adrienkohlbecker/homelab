"""Unit tests for the mosquitto_passwd Jinja filter.

The filter hand-builds mosquitto's $7$ PBKDF2-SHA512 pwfile format (passlib
adapted-base64 re-encoded to standard base64, salt squeezed to mosquitto's
hardcoded 12-byte SALT_LEN). That re-encoding is subtle and otherwise only
exercised end-to-end in the qemu harness, so lock the invariants here.
"""

import base64
import importlib.util
from pathlib import Path

import pytest
from ansible.errors import AnsibleError

_FILTER = Path(__file__).resolve().parents[1] / "roles/mosquitto/filter_plugins/mosquitto_passwd.py"
_spec = importlib.util.spec_from_file_location("mosquitto_passwd", _FILTER)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
mosquitto_passwd = _mod.mosquitto_passwd


def test_emits_mosquitto_v7_format():
    parts = mosquitto_passwd("hunter2", salt="pepper").split("$")
    assert parts[0] == "" and parts[1] == "7"
    assert parts[2] == "210000"
    assert len(parts) == 5


def test_salt_decodes_to_exactly_twelve_bytes():
    b64_salt = mosquitto_passwd("hunter2", salt="pepper").split("$")[3]
    assert len(base64.b64decode(b64_salt)) == 12


def test_checksum_is_standard_padded_base64():
    b64_checksum = mosquitto_passwd("hunter2", salt="pepper").split("$")[4]
    assert len(base64.b64decode(b64_checksum)) == 64


def test_deterministic_for_fixed_salt():
    # The byte-stability the fixed-salt design exists to guarantee: same input
    # -> identical hash, so the pwfile template never churns a restart.
    assert mosquitto_passwd("hunter2", salt="pepper") == mosquitto_passwd("hunter2", salt="pepper")


def test_distinct_salts_yield_distinct_hashes():
    assert mosquitto_passwd("hunter2", salt="pepper") != mosquitto_passwd("hunter2", salt="paprika")


def test_regression_locks_known_vector():
    # If a passlib upgrade changes ab64 decoding or the salt derivation drifts,
    # this breaks loudly instead of silently shipping a pwfile mosquitto rejects.
    assert (
        mosquitto_passwd("hunter2", salt="pepper")
        == "$7$210000$jLvPKdnO+JZ1xfXB$Q7kSXqWH0Dw0ydbfUaFkYE1ARAEAGwnXzpN9jHOK+Dcsto2La3MzlF4vl3r9R3VTcPyfrhU7IyDtVgAVpRFi6A=="
    )


def test_missing_salt_raises():
    with pytest.raises(AnsibleError):
        mosquitto_passwd("hunter2")
