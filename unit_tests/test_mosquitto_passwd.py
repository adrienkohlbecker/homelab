"""Unit tests for the mosquitto_passwd Jinja filter.

Lock the $7$ PBKDF2-SHA512 password-file format, deterministic 12-byte salt,
and padded Base64 encoding; broker acceptance is covered by the QEMU test.
"""

import base64

import pytest
from ansible.errors import AnsibleError

from filter_plugins.mosquitto_passwd import mosquitto_passwd

KNOWN_HASH = (
    "$7$210000$jLvPKdnO+JZ1xfXB$"
    "Q7kSXqWH0Dw0ydbfUaFkYE1ARAEAGwnXzpN9jHOK+Dcsto2La3MzlF4vl3r9R3VTcPyfrhU7IyDtVgAVpRFi6A=="
)


def test_locks_known_hash_and_mosquitto_v7_format():
    result = mosquitto_passwd("hunter2", salt="pepper")
    parts = result.split("$")
    assert result == KNOWN_HASH
    assert parts[0] == ""
    assert parts[1] == "7"
    assert parts[2] == "210000"
    assert len(parts) == 5
    assert len(base64.b64decode(parts[3])) == 12
    assert len(base64.b64decode(parts[4])) == 64


def test_distinct_salts_yield_distinct_hashes():
    assert mosquitto_passwd("hunter2", salt="pepper") != mosquitto_passwd("hunter2", salt="paprika")


def test_missing_salt_raises():
    with pytest.raises(AnsibleError):
        mosquitto_passwd("hunter2")
