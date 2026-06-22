"""Shared Ansible filters for homelab role contracts."""

from __future__ import annotations

import base64
import ipaddress
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from urllib.parse import parse_qs, urlsplit

import yaml
from ansible.errors import AnsibleError


_ANSIBLE_VAR_KEY_RE = re.compile(r"[^A-Za-z0-9_]")
_MISSING = object()


def ansible_var_key(value: Any) -> str:
    """Return a string safe for use as an Ansible dynamic variable name."""
    key = _ANSIBLE_VAR_KEY_RE.sub("_", str(value))
    if not key:
        raise AnsibleError("ansible_var_key requires a non-empty value")
    if not re.match(r"^[A-Za-z_]", key):
        key = "_" + key
    return key


def zfs_mount_unit(mountpoint: str) -> str:
    """Return the historical zfs_mount unit name for an absolute mountpoint."""
    if not isinstance(mountpoint, str) or not mountpoint.startswith("/"):
        raise AnsibleError(f"zfs_mount_unit requires an absolute mountpoint, got {mountpoint!r}")
    if mountpoint == "/root":
        raise AnsibleError("zfs_mount_unit refuses /root because it aliases the root mount unit")

    normalized = "/root" if mountpoint == "/" else mountpoint
    return f"zfs_mount{normalized.replace('/', '_')}.service"


def slurp_text(result: Mapping[str, Any], default: str | None = None, trim: bool = True) -> str:
    """Decode text content from an Ansible slurp result."""
    if "content" not in result:
        if default is not None:
            return default.strip() if trim else default
        raise AnsibleError("slurp_text requires a slurp result with content")

    try:
        text = base64.b64decode(result["content"]).decode()
    except Exception as exc:
        raise AnsibleError(f"slurp_text could not decode slurp content: {exc}") from exc
    return text.strip() if trim else text


def slurp_json(result: Mapping[str, Any], default: Any = _MISSING) -> Any:
    """Decode and parse JSON content from an Ansible slurp result."""
    if "content" not in result and default is not _MISSING:
        return default
    try:
        return json.loads(slurp_text(result))
    except Exception as exc:
        raise AnsibleError(f"slurp_json could not parse slurp content: {exc}") from exc


def slurp_yaml(result: Mapping[str, Any], default: Any = _MISSING) -> Any:
    """Decode and parse YAML content from an Ansible slurp result."""
    if "content" not in result and default is not _MISSING:
        return default
    try:
        return yaml.safe_load(slurp_text(result))
    except Exception as exc:
        raise AnsibleError(f"slurp_yaml could not parse slurp content: {exc}") from exc


def slurp_lines(result: Mapping[str, Any], default: Any = _MISSING) -> list[str]:
    """Decode an Ansible slurp result into text lines."""
    if "content" not in result and default is not _MISSING:
        return default
    return slurp_text(result, trim=False).splitlines()


def rstrip_newlines(value: Any) -> str:
    """Remove only trailing newline characters from rendered text."""
    return str(value).rstrip("\n")


def json_argv(argv: Sequence[Any]) -> str:
    """Render a command argv as a single-quoted compact JSON string."""
    if not isinstance(argv, Sequence) or isinstance(argv, str):
        raise AnsibleError("json_argv expects a sequence of arguments")
    if not argv:
        raise AnsibleError("json_argv requires at least one argument")
    return "'" + json.dumps([str(arg) for arg in argv], separators=(",", ":")) + "'"


def podman_health_curl(
    url: str,
    *,
    location: bool = True,
    fail: bool = True,
    connect_timeout: int = 1,
    max_time: int = 5,
) -> str:
    """Render the repo's standard curl-based podman health probe."""
    argv: list[str] = ["curl"]
    if location:
        argv.append("--location")
    if fail:
        argv.append("--fail")
    argv.extend(
        [
            "--silent",
            "--show-error",
            "--connect-timeout",
            str(connect_timeout),
            "--max-time",
            str(max_time),
            "-o",
            "/dev/null",
            url,
        ]
    )
    return json_argv(argv)


def podman_health_wget(url: str) -> str:
    """Render the repo's standard wget-based podman health probe."""
    return json_argv(
        [
            "wget",
            "--quiet",
            "--tries=1",
            "--timeout=5",
            "-O",
            "/dev/null",
            url,
        ]
    )


def podman_idmap_args(
    user: Mapping[str, Any],
    container_uid: int | str = 0,
    container_gid: int | str | None = None,
) -> list[str]:
    """Return podman uid/gid map args for one in-container identity."""
    host_uid = _get_path(user, "uid")
    host_gid = _get_path(user, "group")
    if host_uid is None or host_gid is None:
        raise AnsibleError("podman_idmap_args requires a user mapping with uid and group")
    if container_gid is None:
        container_gid = container_uid
    return [
        "--uidmap=0:0:65536",
        f"--uidmap=+{container_uid}:{host_uid}:1",
        "--gidmap=0:0:65536",
        f"--gidmap=+{container_gid}:{host_gid}:1",
    ]


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


def host_vlan_block(
    network: Mapping[str, Any],
    inventory_hostname: str,
    vlan: str,
    site: str = "home",
    prefix: int = 28,
    offset: int = 8,
) -> str:
    """Return the per-host VLAN block derived from topology host slot."""
    cidr = _get_path(network, f"sites.{site}.vlans.{vlan}.cidr")
    slot = _get_path(network, f"hosts.{inventory_hostname}.slot")
    if cidr is None or slot is None:
        raise AnsibleError(f"host_vlan_block requires topology cidr and host slot for {inventory_hostname}/{vlan}")

    parent = ipaddress.ip_network(str(cidr), strict=False)
    prefix = int(prefix)
    if prefix < parent.prefixlen:
        raise AnsibleError(f"host_vlan_block prefix /{prefix} is wider than parent {parent}")

    index = int(offset) + int(slot)
    subnet_size = 1 << (parent.max_prefixlen - prefix)
    subnet_address = ipaddress.ip_address(int(parent.network_address) + index * subnet_size)
    subnet = ipaddress.ip_network(f"{subnet_address}/{prefix}", strict=False)
    if not subnet.subnet_of(parent):
        raise AnsibleError(f"host_vlan_block derived {subnet} outside parent {parent}")
    return str(subnet)


def zfs_source_value(stdout: str) -> dict[str, str]:
    """Parse `zfs get -H -p -o source,value` output."""
    fields = str(stdout).splitlines()[0].split("\t")
    if len(fields) != 2:
        raise AnsibleError(f"zfs_source_value expected two tab-separated fields, got {stdout!r}")
    return {"source": fields[0].strip(), "value": fields[1].strip()}


def any_successful_stdout(results: Iterable[Mapping[str, Any]]) -> bool:
    """Return whether any loop result succeeded and produced stdout."""
    return any(result.get("rc") == 0 and bool(result.get("stdout")) for result in results)


def _get_path(item: Any, path: str) -> Any:
    current = item
    for part in path.split("."):
        if isinstance(current, Mapping):
            if part not in current:
                return None
            current = current[part]
        else:
            if not hasattr(current, part):
                return None
            current = getattr(current, part)
    return current


def matching_by_attr(items: Iterable[Any], attr: str | Mapping[str, Any], value: Any = None) -> list[Any]:
    """Return items matching one attribute value or a mapping of criteria."""
    criteria = attr if isinstance(attr, Mapping) else {attr: value}
    return [item for item in items if all(_get_path(item, path) == expected for path, expected in criteria.items())]


def one_by_attr(items: Iterable[Any], attr: str | Mapping[str, Any], value: Any = None) -> Any:
    """Return exactly one matching item, raising when zero or many match."""
    matches = matching_by_attr(items, attr, value)
    if len(matches) != 1:
        raise AnsibleError(f"one_by_attr expected exactly one match, got {len(matches)}")
    return matches[0]


def _nftables(value: str | Mapping[str, Any]) -> list[dict[str, Any]]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping) or not isinstance(value.get("nftables"), list):
        raise AnsibleError("nft filter expects nft JSON with an nftables list")
    return value["nftables"]


def nft_counters_by_name(value: str | Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return standalone nft counter declarations keyed by counter name."""
    counters: dict[str, dict[str, Any]] = {}
    for entry in _nftables(value):
        counter = entry.get("counter")
        if isinstance(counter, Mapping) and "family" in counter and "name" in counter:
            counters[str(counter["name"])] = dict(counter)
    return counters


def nft_rules_by_counter(value: str | Mapping[str, Any], counter_name: str) -> list[dict[str, Any]]:
    """Return nft rules whose expression references a named counter."""
    rules: list[dict[str, Any]] = []
    for entry in _nftables(value):
        rule = entry.get("rule")
        if not isinstance(rule, Mapping):
            continue
        expr = rule.get("expr")
        if isinstance(expr, list) and {"counter": counter_name} in expr:
            rules.append(dict(rule))
    return rules


def nft_rule_by_counter(value: str | Mapping[str, Any], counter_name: str) -> dict[str, Any]:
    """Return exactly one nft rule referencing a named counter."""
    rules = nft_rules_by_counter(value, counter_name)
    if len(rules) != 1:
        raise AnsibleError(f"nft_rule_by_counter expected one rule for {counter_name}, got {len(rules)}")
    return rules[0]


class FilterModule:
    def filters(self):
        return {
            "ansible_var_key": ansible_var_key,
            "any_successful_stdout": any_successful_stdout,
            "authelia_redirects_to": authelia_redirects_to,
            "host_vlan_block": host_vlan_block,
            "json_argv": json_argv,
            "matching_by_attr": matching_by_attr,
            "nft_counters_by_name": nft_counters_by_name,
            "nft_rule_by_counter": nft_rule_by_counter,
            "nft_rules_by_counter": nft_rules_by_counter,
            "one_by_attr": one_by_attr,
            "only_by_attr": one_by_attr,
            "podman_health_curl": podman_health_curl,
            "podman_health_wget": podman_health_wget,
            "podman_idmap_args": podman_idmap_args,
            "rstrip_newlines": rstrip_newlines,
            "slurp_json": slurp_json,
            "slurp_lines": slurp_lines,
            "slurp_text": slurp_text,
            "slurp_yaml": slurp_yaml,
            "zfs_source_value": zfs_source_value,
            "zfs_mount_unit": zfs_mount_unit,
        }
