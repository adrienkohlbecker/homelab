"""Shared Ansible tests for homelab role contracts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit


def any_successful_stdout(results: Iterable[Mapping[str, Any]]) -> bool:
    """Return whether any loop result succeeded and produced stdout."""
    return any(result.get("rc") == 0 and bool(result.get("stdout")) for result in results)


def authelia_redirects_to(
    result: Mapping[str, Any],
    subdomain: str,
    inventory_hostname: str,
    domain: str,
    require_rd: bool = True,
) -> bool:
    """Return whether a URI result is an Authelia redirect for a service."""
    location = str(result.get("location", ""))
    if result.get("status") != 302:
        return False

    auth_url = f"https://auth.{inventory_hostname}.{domain}"
    if not location.startswith(auth_url):
        return False

    if not require_rd:
        return True

    service_url = f"https://{subdomain}.{inventory_hostname}.{domain}"
    query = parse_qs(urlsplit(location).query, keep_blank_values=True)
    return service_url in query.get("rd", []) or f"rd={service_url}" in location


class TestModule:
    def tests(self):
        return {
            "any_successful_stdout": any_successful_stdout,
            "authelia_redirects_to": authelia_redirects_to,
        }
