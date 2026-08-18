"""Filters for Transmission's salted RPC password format."""

import hashlib
import re

TRANSMISSION_PASSWORD_RE = re.compile(r"^\{(?P<digest>[0-9a-f]{40})(?P<salt>.+)$")


def transmission_password_is_salted_hash(value):
    """Return whether value uses Transmission's persisted password format."""
    return isinstance(value, str) and TRANSMISSION_PASSWORD_RE.fullmatch(value) is not None


def transmission_password_matches(salted_password, plaintext):
    """Return whether plaintext matches a Transmission salted SHA-1 hash."""
    if not isinstance(salted_password, str):
        return False

    match = TRANSMISSION_PASSWORD_RE.fullmatch(salted_password)
    if match is None:
        return False

    salt = match.group("salt")
    digest = hashlib.sha1((str(plaintext) + salt).encode()).hexdigest()
    return match.group("digest") == digest


class FilterModule:
    def filters(self):
        return {
            "transmission_password_is_salted_hash": transmission_password_is_salted_hash,
            "transmission_password_matches": transmission_password_matches,
        }
