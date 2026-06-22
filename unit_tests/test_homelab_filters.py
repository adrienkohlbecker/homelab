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
    assert homelab.slurp_text(result) == "hello"


def test_slurp_text_can_preserve_surrounding_whitespace() -> None:
    result = {"content": base64.b64encode(b"hello\n").decode()}
    assert homelab.slurp_text(result, trim=False) == "hello\n"


def test_slurp_text_uses_default_for_missing_content() -> None:
    assert homelab.slurp_text({}, default="{}") == "{}"


def test_slurp_text_rejects_missing_content_without_default() -> None:
    with pytest.raises(AnsibleError):
        homelab.slurp_text({})


def test_slurp_json_parses_slurp_content() -> None:
    result = {"content": base64.b64encode(b'{"enabled": true}').decode()}
    assert homelab.slurp_json(result) == {"enabled": True}


def test_slurp_json_uses_default_for_missing_content() -> None:
    assert homelab.slurp_json({}, default={}) == {}


def test_slurp_yaml_parses_slurp_content() -> None:
    result = {"content": base64.b64encode(b"jobs:\n  - name: cert\n").decode()}
    assert homelab.slurp_yaml(result) == {"jobs": [{"name": "cert"}]}


def test_slurp_lines_preserves_line_content() -> None:
    result = {"content": base64.b64encode(b"one\n\n two \n").decode()}
    assert homelab.slurp_lines(result) == ["one", "", " two "]


def test_rstrip_newlines_removes_only_trailing_newline_characters() -> None:
    assert homelab.rstrip_newlines("value  \n\n") == "value  "


def test_json_argv_renders_compact_json_in_single_quotes() -> None:
    assert homelab.json_argv(["extra/healthcheck"]) == "'[\"extra/healthcheck\"]'"


def test_podman_health_curl_renders_repo_default_probe() -> None:
    assert homelab.podman_health_curl("http://localhost:8989/ping") == (
        '\'["curl","--location","--fail","--silent","--show-error",'
        '"--connect-timeout","1","--max-time","5","-o","/dev/null",'
        '"http://localhost:8989/ping"]\''
    )


def test_podman_health_curl_can_skip_fail_and_location() -> None:
    rendered = homelab.podman_health_curl("http://127.0.0.1:9091/transmission/rpc", location=False, fail=False)
    argv = json.loads(rendered.strip("'"))
    assert "--location" not in argv
    assert "--fail" not in argv
    assert argv[-1] == "http://127.0.0.1:9091/transmission/rpc"


def test_podman_health_wget_renders_get_probe_to_dev_null() -> None:
    rendered = homelab.podman_health_wget("http://localhost:5055/api/v1/status")
    assert json.loads(rendered.strip("'")) == [
        "wget",
        "--quiet",
        "--tries=1",
        "--timeout=5",
        "-O",
        "/dev/null",
        "http://localhost:5055/api/v1/status",
    ]


def test_podman_idmap_args_maps_one_container_identity() -> None:
    assert homelab.podman_idmap_args({"uid": 120001, "group": 120002}, container_uid=1000) == [
        "--uidmap=0:0:65536",
        "--uidmap=+1000:120001:1",
        "--gidmap=0:0:65536",
        "--gidmap=+1000:120002:1",
    ]


def test_podman_idmap_args_allows_distinct_container_gid() -> None:
    assert (
        homelab.podman_idmap_args({"uid": 120001, "group": 120002}, container_uid=1000, container_gid=2000)[-1]
        == "--gidmap=+2000:120002:1"
    )


def test_authelia_redirects_to_checks_status_auth_host_and_rd() -> None:
    result = {
        "status": 302,
        "location": "https://auth.box.example.test/?rd=https://bazarr.box.example.test",
    }
    assert homelab.authelia_redirects_to(result, "bazarr", "box", "example.test")
    assert not homelab.authelia_redirects_to(result, "radarr", "box", "example.test")


def test_authelia_redirects_to_can_skip_rd_check() -> None:
    result = {"status": 302, "location": "https://auth.box.example.test/"}
    assert homelab.authelia_redirects_to(result, "kuma", "box", "example.test", require_rd=False)


def test_host_vlan_block_derives_slot_indexed_subnet() -> None:
    network = {
        "sites": {"home": {"vlans": {"management": {"cidr": "10.123.0.0/23"}, "iot": {"cidr": "10.123.4.0/24"}}}},
        "hosts": {"lab": {"slot": 0}, "pug": {"slot": 1}, "box": {"slot": 3}},
    }
    assert homelab.host_vlan_block(network, "lab", "management") == "10.123.0.128/28"
    assert homelab.host_vlan_block(network, "pug", "management") == "10.123.0.144/28"
    assert homelab.host_vlan_block(network, "box", "iot") == "10.123.4.176/28"


def test_zfs_source_value_parses_tab_separated_source_and_value() -> None:
    assert homelab.zfs_source_value("local\t/mnt/media\n") == {"source": "local", "value": "/mnt/media"}


def test_any_successful_stdout_finds_successful_nonempty_result() -> None:
    assert homelab.any_successful_stdout([{"rc": 1, "stdout": ""}, {"rc": 0, "stdout": "10.0.0.1"}])
    assert not homelab.any_successful_stdout([{"rc": 0, "stdout": ""}, {"rc": 1, "stdout": "ignored"}])


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
    assert filters["any_successful_stdout"] is homelab.any_successful_stdout
    assert filters["authelia_redirects_to"] is homelab.authelia_redirects_to
    assert filters["host_vlan_block"] is homelab.host_vlan_block
    assert filters["json_argv"] is homelab.json_argv
    assert filters["only_by_attr"] is homelab.one_by_attr
    assert filters["podman_health_wget"] is homelab.podman_health_wget
    assert filters["rstrip_newlines"] is homelab.rstrip_newlines
    assert filters["slurp_json"] is homelab.slurp_json
    assert filters["slurp_lines"] is homelab.slurp_lines
    assert filters["slurp_yaml"] is homelab.slurp_yaml
    assert filters["zfs_source_value"] is homelab.zfs_source_value
    assert filters["zfs_mount_unit"] is homelab.zfs_mount_unit
