"""Unit tests for shared homelab Ansible filters."""

import json

import pytest
from ansible.errors import AnsibleError

from filter_plugins import homelab
from test_plugins import homelab as homelab_tests


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


def test_podman_health_curl_can_skip_location() -> None:
    rendered = homelab.podman_health_curl("http://localhost:8080/health", location=False)
    argv = json.loads(rendered.strip("'"))
    assert "--location" not in argv
    assert "--fail" in argv
    assert argv[-1] == "http://localhost:8080/health"


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
    assert homelab.podman_idmap_args({"uid": 120001, "gid": 120002}, container_uid=1000) == [
        "--uidmap=0:0:65536",
        "--uidmap=+1000:120001:1",
        "--gidmap=0:0:65536",
        "--gidmap=+1000:120002:1",
    ]


def test_authelia_redirects_to_checks_status_auth_host_and_rd() -> None:
    result = {
        "status": 302,
        "location": "https://auth.box.example.test/?rd=https://bazarr.box.example.test",
    }
    assert homelab_tests.authelia_redirects_to(result, "bazarr", "box", "example.test")
    assert not homelab_tests.authelia_redirects_to(result, "radarr", "box", "example.test")


def test_authelia_redirects_to_can_skip_rd_check() -> None:
    result = {"status": 302, "location": "https://auth.box.example.test/"}
    assert homelab_tests.authelia_redirects_to(result, "kuma", "box", "example.test", require_rd=False)


def test_host_vlan_block_derives_slot_indexed_subnet() -> None:
    network = {
        "sites": {
            "home": {"vlans": {"management": {"cidr": "10.123.0.0/23"}, "iot": {"cidr": "10.123.4.0/24"}}},
            "remote": {"vlans": {"management": {"cidr": "10.124.0.0/23"}}},
        },
        "hosts": {
            "lab": {"site": "home", "slot": 0},
            "pug": {"site": "home", "slot": 1},
            "box": {"site": "home", "slot": 3},
            "bunk": {"site": "remote", "slot": 0},
        },
    }
    assert homelab.host_vlan_block(network, "lab", "management") == "10.123.0.128/28"
    assert homelab.host_vlan_block(network, "pug", "management") == "10.123.0.144/28"
    assert homelab.host_vlan_block(network, "box", "iot") == "10.123.4.176/28"
    assert homelab.host_vlan_block(network, "bunk", "management") == "10.124.0.128/28"


@pytest.mark.parametrize(
    ("cidr", "slot", "error"),
    [
        ("invalid", 0, ValueError),
        ("10.123.4.0/29", 0, AnsibleError),
        ("10.123.4.0/24", 8, AnsibleError),
    ],
)
def test_host_vlan_block_rejects_invalid_subnets(cidr: str, slot: int, error: type[Exception]) -> None:
    network = {
        "sites": {"home": {"vlans": {"iot": {"cidr": cidr}}}},
        "hosts": {"lab": {"site": "home", "slot": slot}},
    }
    with pytest.raises(error):
        homelab.host_vlan_block(network, "lab", "iot")


def test_host_vlan_block_requires_host_site() -> None:
    network = {
        "sites": {"home": {"vlans": {"iot": {"cidr": "10.123.4.0/24"}}}},
        "hosts": {"lab": {"slot": 0}},
    }
    with pytest.raises(AnsibleError, match="requires topology site"):
        homelab.host_vlan_block(network, "lab", "iot")


def test_any_successful_stdout_finds_successful_nonempty_result() -> None:
    assert homelab_tests.any_successful_stdout([{"rc": 1, "stdout": ""}, {"rc": 0, "stdout": "10.0.0.1"}])
    assert not homelab_tests.any_successful_stdout([{"rc": 0, "stdout": ""}, {"rc": 1, "stdout": "ignored"}])


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
    assert "any_successful_stdout" not in filters
    assert "authelia_redirects_to" not in filters
    assert filters["host_vlan_block"] is homelab.host_vlan_block
    assert filters["json_argv"] is homelab.json_argv
    assert filters["podman_health_wget"] is homelab.podman_health_wget
    assert filters["rstrip_newlines"] is homelab.rstrip_newlines
    assert filters["zfs_mount_unit"] is homelab.zfs_mount_unit


def test_exposes_tests() -> None:
    tests = homelab_tests.TestModule().tests()
    assert tests["any_successful_stdout"] is homelab_tests.any_successful_stdout
    assert tests["authelia_redirects_to"] is homelab_tests.authelia_redirects_to
