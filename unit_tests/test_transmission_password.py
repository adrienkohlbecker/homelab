"""Unit tests for Transmission salted RPC password validation."""

from filter_plugins.transmission_password import (
    transmission_password_is_salted_hash,
    transmission_password_matches,
)


def test_recognizes_transmission_hash():
    assert transmission_password_is_salted_hash("{69acd5c5300fcfd6a9871f5018f82325e9036210salty123")


def test_rejects_non_hash_password_values():
    assert not transmission_password_is_salted_hash("plaintext")
    assert not transmission_password_is_salted_hash("{short")
    assert not transmission_password_is_salted_hash(None)


def test_matching_transmission_hash():
    assert transmission_password_matches(
        "{69acd5c5300fcfd6a9871f5018f82325e9036210salty123",
        "correct horse battery staple",
    )


def test_rejects_wrong_password():
    assert not transmission_password_matches(
        "{69acd5c5300fcfd6a9871f5018f82325e9036210salty123",
        "wrong password",
    )


def test_rejects_plaintext_and_malformed_hashes():
    assert not transmission_password_matches("plaintext", "plaintext")
    assert not transmission_password_matches("{short", "password")
    assert not transmission_password_matches(None, "password")
