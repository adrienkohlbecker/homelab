"""Unit tests for shared homelab Ansible filters."""

import base64
import json

import pytest
from ansible.errors import AnsibleError

from filter_plugins import homelab


def test_ansible_var_key_sanitizes_and_guards_start() -> None:
    assert homelab.ansible_var_key("kuma.service") == "kuma_service"
    assert homelab.ansible_var_key("netdata-charts.d") == "netdata_charts_d"
    assert homelab.ansible_var_key("3proxy") == "_3proxy"


def test_ansible_var_key_rejects_empty_values() -> None:
    with pytest.raises(AnsibleError):
        homelab.ansible_var_key("")


def test_zfs_mount_unit_preserves_historical_mapping() -> None:
    assert homelab.zfs_mount_unit("/") == "zfs_mount_root.service"
    assert homelab.zfs_mount_unit("/mnt/services") == "zfs_mount_mnt_services.service"
    assert homelab.zfs_mount_unit("/mnt/services/sqlite") == "zfs_mount_mnt_services_sqlite.service"


@pytest.mark.parametrize("mountpoint", ["mnt/services", "/root"])
def test_zfs_mount_unit_rejects_invalid_or_ambiguous_mountpoints(mountpoint: str) -> None:
    with pytest.raises(AnsibleError):
        homelab.zfs_mount_unit(mountpoint)


def test_slurp_text_decodes_content() -> None:
    result = {"content": base64.b64encode(b"hello\n").decode()}
    assert homelab.slurp_text(result) == "hello\n"


def test_slurp_text_uses_default_for_missing_content() -> None:
    assert homelab.slurp_text({}, default="{}") == "{}"


def test_slurp_text_rejects_missing_content_without_default() -> None:
    with pytest.raises(AnsibleError):
        homelab.slurp_text({})


def test_podman_health_argv_renders_compact_json_in_single_quotes() -> None:
    assert homelab.podman_health_argv(["extra/healthcheck"]) == "'[\"extra/healthcheck\"]'"


def test_podman_health_curl_renders_repo_default_probe() -> None:
    assert homelab.podman_health_curl("http://localhost:8989/ping") == (
        '\'["curl","--location","--fail","--silent","--show-error",'
        '"--connect-timeout","1","--max-time","5","-o","/dev/null",'
        '"http://localhost:8989/ping"]\''
    )


def test_podman_health_curl_supports_head_and_request() -> None:
    rendered = homelab.podman_health_curl("http://localhost:8123/manifest.json", head=True, request="GET")
    argv = json.loads(rendered.strip("'"))
    assert argv[:4] == ["curl", "--head", "--request", "GET"]
    assert argv[-1] == "http://localhost:8123/manifest.json"


def test_podman_idmap_args_maps_one_container_identity() -> None:
    assert homelab.podman_idmap_args(1000, 120001, 120002) == [
        "--uidmap=0:0:65536",
        "--uidmap=+1000:120001:1",
        "--gidmap=0:0:65536",
        "--gidmap=+1000:120002:1",
    ]


def test_podman_idmap_args_allows_distinct_container_gid() -> None:
    assert homelab.podman_idmap_args(1000, 120001, 120002, container_gid=2000)[-1] == "--gidmap=+2000:120002:1"


def test_podman_secret_file_args_pairs_target_with_env_path() -> None:
    assert homelab.podman_secret_file_args(
        "authelia_session_secret", "session_secret", "AUTHELIA_SESSION_SECRET_FILE"
    ) == [
        "--secret=authelia_session_secret,type=mount,target=session_secret",
        "--env AUTHELIA_SESSION_SECRET_FILE=/run/secrets/session_secret",
    ]


def test_one_by_attr_supports_simple_and_nested_paths() -> None:
    items = [{"name": "servers", "user": {"name": "infra"}}, {"name": "clients", "user": {"name": "infra"}}]
    assert homelab.one_by_attr(items, "name", "servers") == items[0]
    assert homelab.one_by_attr(items, {"user.name": "infra", "name": "clients"}) == items[1]


def test_one_by_attr_rejects_zero_or_multiple_matches() -> None:
    with pytest.raises(AnsibleError):
        homelab.one_by_attr([], "name", "missing")
    with pytest.raises(AnsibleError):
        homelab.one_by_attr([{"name": "same"}, {"name": "same"}], "name", "same")


def test_nft_helpers_extract_counters_and_rules_by_counter_reference() -> None:
    payload = {
        "nftables": [
            {"counter": {"family": "inet", "table": "filter", "name": "input_http", "packets": 2}},
            {"rule": {"expr": [{"counter": "input_http"}, {"accept": None}]}},
            {"rule": {"expr": [{"counter": "other"}, {"accept": None}]}},
        ]
    }
    assert homelab.nft_counters_by_name(payload)["input_http"]["packets"] == 2
    assert homelab.nft_rule_by_counter(payload, "input_http")["expr"] == [{"counter": "input_http"}, {"accept": None}]
    assert homelab.nft_rules_by_counter(payload, "missing") == []


def test_exposes_filters() -> None:
    filters = homelab.FilterModule().filters()
    assert filters["ansible_var_key"] is homelab.ansible_var_key
    assert filters["only_by_attr"] is homelab.one_by_attr
    assert filters["zfs_mount_unit"] is homelab.zfs_mount_unit
