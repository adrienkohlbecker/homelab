"""Filters for Transmission's salted RPC password format."""

import hashlib


def transmission_password_matches(salted_password, plaintext):
    """Return whether plaintext matches a Transmission salted SHA-1 hash."""
    if not isinstance(salted_password, str) or not salted_password.startswith("{"):
        return False
    if len(salted_password) < 41:
        return False

    salt = salted_password[41:]
    digest = hashlib.sha1((str(plaintext) + salt).encode()).hexdigest()
    return salted_password == "{" + digest + salt


class FilterModule:
    def filters(self):
        return {
            "transmission_password_matches": transmission_password_matches,
        }
