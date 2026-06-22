"""Shared Ansible filters for homelab role contracts."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ansible.errors import AnsibleError


_ANSIBLE_VAR_KEY_RE = re.compile(r"[^A-Za-z0-9_]")


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


def slurp_text(result: Mapping[str, Any], default: str | None = None) -> str:
    """Decode text content from an Ansible slurp result."""
    if "content" not in result:
        if default is not None:
            return default
        raise AnsibleError("slurp_text requires a slurp result with content")

    try:
        return base64.b64decode(result["content"]).decode()
    except Exception as exc:
        raise AnsibleError(f"slurp_text could not decode slurp content: {exc}") from exc


def podman_health_argv(argv: Sequence[Any]) -> str:
    """Render a podman health command as a single-quoted JSON argv string."""
    if not isinstance(argv, Sequence) or isinstance(argv, str):
        raise AnsibleError("podman_health_argv expects a sequence of arguments")
    if not argv:
        raise AnsibleError("podman_health_argv requires at least one argument")
    return "'" + json.dumps([str(arg) for arg in argv], separators=(",", ":")) + "'"


def podman_health_curl(
    url: str,
    *,
    head: bool = False,
    request: str | None = None,
    location: bool = True,
    fail: bool = True,
    connect_timeout: int = 1,
    max_time: int = 5,
) -> str:
    """Render the repo's standard curl-based podman health probe."""
    argv: list[str] = ["curl"]
    if head:
        argv.append("--head")
    if request is not None:
        argv.extend(["--request", request])
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
    return podman_health_argv(argv)


def podman_idmap_args(
    container_uid: int | str,
    host_uid: int | str,
    host_gid: int | str,
    container_gid: int | str | None = None,
) -> list[str]:
    """Return podman uid/gid map args for one in-container identity."""
    if container_gid is None:
        container_gid = container_uid
    return [
        "--uidmap=0:0:65536",
        f"--uidmap=+{container_uid}:{host_uid}:1",
        "--gidmap=0:0:65536",
        f"--gidmap=+{container_gid}:{host_gid}:1",
    ]


def podman_secret_file_args(secret: str, target: str, env: str) -> list[str]:
    """Return podman secret mount plus matching file-env argument."""
    if not secret or not target or not env:
        raise AnsibleError("podman_secret_file_args requires secret, target, and env")
    return [
        f"--secret={secret},type=mount,target={target}",
        f"--env {env}=/run/secrets/{target}",
    ]


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
            "matching_by_attr": matching_by_attr,
            "nft_counters_by_name": nft_counters_by_name,
            "nft_rule_by_counter": nft_rule_by_counter,
            "nft_rules_by_counter": nft_rules_by_counter,
            "one_by_attr": one_by_attr,
            "only_by_attr": one_by_attr,
            "podman_health_argv": podman_health_argv,
            "podman_health_curl": podman_health_curl,
            "podman_idmap_args": podman_idmap_args,
            "podman_secret_file_args": podman_secret_file_args,
            "slurp_text": slurp_text,
            "zfs_mount_unit": zfs_mount_unit,
        }
