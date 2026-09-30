"""Health and scan regressions for both supported ZFS status formats."""

import copy
import json
import subprocess

import pytest
from conftest import load_repo_module

status = load_repo_module("roles/zfs/files/zfs_status.py")


@pytest.fixture
def pool():
    return {
        "name": "tank",
        "state": "ONLINE",
        "error_count": 0,
        "vdevs": {
            "mirror-0": {"read_errors": 0, "write_errors": 0, "checksum_errors": 0},
            "disk0": {"read_errors": 0, "write_errors": 0, "checksum_errors": 0, "slow_ios": 0},
        },
        "scan_stats": {
            "function": "SCRUB",
            "state": "FINISHED",
            "start_time": 80,
            "end_time": 90,
            "scrub_pause": 0,
        },
    }


def document(pools):
    return json.dumps({"output_version": {"command": "zpool status", "vers_major": 0, "vers_minor": 1}, "pools": pools})


def test_parses_integer_counters_without_rounding(pool):
    pool["vdevs"]["disk0"]["checksum_errors"] = 2**60 + 1
    assert status.parse_status(document({"tank": pool}))["tank"]["vdevs"]["disk0"]["checksum_errors"] == 2**60 + 1


def test_no_imported_pools_is_valid():
    assert status.parse_status(document({})) == {}


def test_accepts_additive_minor_schema_changes(pool):
    payload = json.loads(document({"tank": pool}))
    payload["output_version"].update(vers_minor=2, new_metadata="ignored")
    payload["pools"]["tank"]["new_stat"] = 42
    assert status.parse_status(json.dumps(payload)) == {"tank": pool | {"new_stat": 42}}


@pytest.mark.parametrize(("field", "value"), [("vers_major", 1), ("vers_minor", 0), ("vers_minor", True)])
def test_rejects_incompatible_or_malformed_schema_versions(field, value):
    payload = json.loads(document({}))
    payload["output_version"][field] = value
    with pytest.raises(status.StatusError):
        status.parse_status(json.dumps(payload))


def test_never_scrubbed_pool_and_spare_are_valid(pool):
    del pool["scan_stats"]
    pool["spares"] = {"spare0": {"state": "AVAIL"}}
    assert status.parse_status(document({"tank": pool})) == {"tank": pool}


@pytest.mark.parametrize("state", ["AVAIL", "INUSE", "FAULTED", "REMOVED", "CANT_OPEN"])
def test_spare_states_have_no_io_counters(pool, state):
    pool["spares"] = {"spare0": {"state": state, "class": "spare"}}
    assert status.parse_status(document({"tank": pool})) == {"tank": pool}


@pytest.mark.parametrize("group", ["vdevs", "dedup", "special", "logs", "l2cache"])
def test_nested_vdevs_preserve_active_spare_counters(pool, group):
    device = pool["vdevs"].pop("disk0")
    device["checksum_errors"] = 5
    pool[group] = {"mirror-0": {**pool["vdevs"]["mirror-0"], "vdevs": {"spare0": device}}}
    pool["spares"] = {"spare0": {"state": "INUSE"}}
    parsed = status.parse_status(document({"tank": pool}))["tank"]
    assert any(vdev["checksum_errors"] == 5 for _, vdev in status.pool_vdevs(parsed))
    device["checksum_errors"] = "5"
    with pytest.raises(status.StatusError, match="checksum_errors"):
        status.parse_status(document({"tank": pool}))


def test_status_keeps_the_vdev_tree_separate_from_spares(monkeypatch, pool):
    calls = []

    def run(*argv):
        calls.append(argv)
        return document({"tank": pool})

    monkeypatch.setattr(status, "run", run)
    assert status.read_status("tank") == {"tank": pool}
    assert "--json-flat-vdevs" not in calls[0]


@pytest.mark.parametrize("value", [None, "1K", "0", True, -1])
def test_rejects_missing_or_inexact_counters(pool, value):
    pool["vdevs"]["disk0"]["read_errors"] = value
    with pytest.raises(status.StatusError, match="read_errors"):
        status.parse_status(document({"tank": pool}))


@pytest.mark.parametrize("value", [{}, {"output_version": {}}, {"output_version": {"vers_major": 1}, "pools": {}}])
def test_rejects_malformed_or_unknown_schema(value):
    with pytest.raises(status.StatusError):
        status.parse_status(json.dumps(value))


def test_rejects_missing_requested_pool(monkeypatch):
    monkeypatch.setattr(status, "run", lambda *_: document({}))
    with pytest.raises(status.StatusError, match="Requested pools"):
        status.read_status("tank")


def test_explain_accepts_a_healthy_requested_pool_being_omitted(monkeypatch):
    monkeypatch.setattr(status, "run", lambda *_: document({}))
    assert status.read_status("tank", explain=True) == {}


def test_explain_rejects_unrequested_pools(monkeypatch, pool):
    monkeypatch.setattr(status, "run", lambda *_: document({"tank": pool}))
    with pytest.raises(status.StatusError, match="Requested pools"):
        status.read_status("other", explain=True)


@pytest.mark.parametrize(
    ("function", "state", "pause", "active", "scrub_only"),
    [
        ("SCRUB", "SCANNING", 0, True, True),
        ("SCRUB", "SCANNING", 95, False, True),
        ("SCRUB", "CANCELED", 95, False, False),
        ("SCRUB", "FINISHED", 0, False, False),
        ("RESILVER", "SCANNING", 0, True, False),
        ("RESILVER", "SCANNING", 0, False, True),
        ("RESILVER", "FINISHED", 0, False, False),
    ],
)
def test_scan_activity_distinguishes_pause_and_history(pool, function, state, pause, active, scrub_only):
    pool["scan_stats"].update(function=function, state=state, scrub_pause=pause)
    assert status.scan_active(pool, scrub_only=scrub_only) is active


def test_sequential_resilver_is_active_but_not_a_scrub(pool):
    pool["scan_stats"] = {"rebuild_stats": {"mirror-0": {"state": "ACTIVE"}}}
    parsed = status.parse_status(document({"tank": pool}))["tank"]
    assert status.scan_active(parsed)
    assert not status.scan_active(parsed, scrub_only=True)


@pytest.mark.parametrize("field", ["state", "function", "scrub_pause"])
def test_rejects_unknown_or_incomplete_scan(pool, field):
    pool["scan_stats"][field] = "unexpected"
    with pytest.raises(status.StatusError):
        status.parse_status(document({"tank": pool}))


@pytest.mark.parametrize("value", [{}, [], None])
def test_malformed_scan_state_is_a_countable_status_error(pool, value):
    pool["scan_stats"]["state"] = value
    with pytest.raises(status.StatusError, match="scan state"):
        status.parse_status(document({"tank": pool}))


@pytest.mark.parametrize(
    ("version", "supported"), [("zfs-2.2.2-0ubuntu9.5", False), ("zfs-2.3.0", True), ("zfs-2.4.1-1ubuntu5.1", True)]
)
def test_selects_the_userspace_version(monkeypatch, version, supported):
    monkeypatch.setattr(status, "run", lambda *_: version + "\nzfs-kmod-2.2.2\n")
    assert status.supports_json() is supported


@pytest.mark.parametrize("json_supported", [True, False])
def test_health_launcher_selects_the_parser_at_runtime(monkeypatch, json_supported):
    monkeypatch.setattr(status.sys, "argv", ["zfs_health"])
    monkeypatch.setattr(status, "supports_json", lambda: json_supported)
    calls = []
    monkeypatch.setattr(status, "health", lambda: calls.append("json") or 0)

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs == {"check": False}
        return subprocess.CompletedProcess(argv, 7)

    monkeypatch.setattr(status.subprocess, "run", run)
    assert status.main() == (0 if json_supported else 7)
    assert calls == (["json"] if json_supported else [["/opt/zfs/zfs_health_legacy.sh"]])


def test_unknown_version_fails_closed(monkeypatch):
    monkeypatch.setattr(status, "run", lambda *_: "unexpected")
    with pytest.raises(status.StatusError, match="userspace version"):
        status.supports_json()


def test_noble_scan_fallback_pins_locale_and_does_not_request_json(monkeypatch):
    monkeypatch.setattr(status, "supports_json", lambda: False)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="scan: scrub in progress", stderr="")

    monkeypatch.setattr(status.subprocess, "run", run)
    assert status.scan_in_progress("tank")
    assert calls[0][0] == ["timeout", "-k", "10", "60", "zpool", "status", "tank"]
    assert calls[0][1]["env"]["LC_ALL"] == "C"


def test_scan_query_failure_propagates(monkeypatch):
    monkeypatch.setattr(status, "supports_json", lambda: True)

    def fail(*_, **__):
        raise subprocess.CalledProcessError(124, ["zpool"], stderr="pool wedged")

    monkeypatch.setattr(status, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        status.scan_in_progress(None)


@pytest.mark.parametrize(
    ("state", "pause", "end", "expected"),
    [
        ("FINISHED", 0, 90, None),
        ("FINISHED", 0, 70, "expired"),
        ("SCANNING", 70, 0, "expired"),
        ("SCANNING", 0, 0, None),
        ("CANCELED", 95, 0, "canceled"),
    ],
)
def test_scrub_watchdog_uses_integer_timestamps(pool, state, pause, end, expected):
    pool["scan_stats"].update(state=state, scrub_pause=pause, end_time=end)
    issue = status.scrub_issue("tank", pool, now=100, expire=20)
    assert issue is None if expected is None else expected in issue


def test_creation_baseline_without_a_recorded_scan(monkeypatch, pool):
    pool.pop("scan_stats")
    monkeypatch.setattr(status, "run", lambda *_: "70\n")
    assert status.scrub_issue("tank", pool, now=100, expire=20) == "Scrub expired on tank"


@pytest.mark.parametrize(("end", "expected"), [(90, None), (70, "Scrub expired on tank")])
def test_age_since_completed_resilver_replaces_creation_baseline(monkeypatch, pool, end, expected):
    pool["scan_stats"].update(function="RESILVER", state="FINISHED", end_time=end)

    def unexpected_creation_query(*_):
        pytest.fail("A completed resilver must use its end_time, not pool creation")

    monkeypatch.setattr(status, "run", unexpected_creation_query)
    assert status.scrub_issue("tank", pool, now=100, expire=20) == expected


@pytest.mark.parametrize(("end", "expected"), [(95, None), (70, "Scrub expired on tank")])
def test_age_since_completed_sequential_resilver(pool, end, expected):
    pool["scan_stats"] = {"rebuild_stats": {"mirror-0": {"state": "COMPLETE", "end_time": end}}}
    parsed = status.parse_status(document({"tank": pool}))["tank"]
    assert status.scrub_issue("tank", parsed, now=100, expire=20) == expected


def test_latest_completion_wins_when_scan_and_rebuild_history_coexist(pool):
    pool["scan_stats"]["end_time"] = 70
    pool["scan_stats"]["rebuild_stats"] = {"mirror-0": {"state": "COMPLETE", "end_time": 95}}
    assert status.scrub_issue("tank", pool, now=100, expire=20) is None
    pool["scan_stats"]["end_time"] = 99
    assert status.scrub_issue("tank", pool, now=110, expire=15) is None


def test_paused_scrub_timestamp_is_not_hidden_by_old_rebuild_history(pool):
    pool["scan_stats"].update(state="SCANNING", scrub_pause=95)
    pool["scan_stats"]["rebuild_stats"] = {"mirror-0": {"state": "COMPLETE", "end_time": 70}}
    assert status.scrub_issue("tank", pool, now=100, expire=20) is None


def test_health_mails_faults_and_includes_slow_io_diagnostics(monkeypatch, pool, capsys):
    pool["vdevs"]["disk0"]["checksum_errors"] = 5
    healthy = copy.deepcopy(pool)
    healthy["vdevs"]["disk0"]["checksum_errors"] = 0
    monkeypatch.setattr(status.os, "geteuid", lambda: 0)
    monkeypatch.setattr(status.time, "time", lambda: 100)
    monkeypatch.setenv("SCRUB_EXPIRE", "20")
    monkeypatch.setattr(status, "run", lambda *args: "tank\n" if "list" in args else "SLOW\ndisk0 ONLINE 0 0 5 3")
    monkeypatch.setattr(
        status, "read_status", lambda *_, **kwargs: {"tank": healthy} if kwargs.get("explain") else {"tank": pool}
    )
    mail = []
    monkeypatch.setattr(status.subprocess, "run", lambda *args, **kwargs: mail.append((args, kwargs)))
    assert status.health() == 1
    assert "Detected drive errors (READ/WRITE/CKSUM)" in capsys.readouterr().err
    assert len(mail) == 1
    assert mail[0][0][0][0] == "mail"
    assert "SLOW" in mail[0][1]["input"]


def test_health_query_failure_is_mailed(monkeypatch, capsys):
    monkeypatch.setattr(status.os, "geteuid", lambda: 0)
    monkeypatch.setattr(status, "run", lambda *args: "tank\n" if "list" in args else "report")

    def fail(*_, **__):
        raise status.StatusError("bad JSON schema")

    monkeypatch.setattr(status, "read_status", fail)
    mail = []
    monkeypatch.setattr(status.subprocess, "run", lambda *args, **kwargs: mail.append(kwargs["input"]))
    assert status.health() == 1
    assert "bad JSON schema" in capsys.readouterr().err
    assert len(mail) == 1


def test_health_accepts_feature_capped_pools_and_does_not_alarm_on_slow_io(monkeypatch, pool):
    pool["vdevs"]["disk0"]["slow_ios"] = 3
    monkeypatch.setattr(status.os, "geteuid", lambda: 0)
    monkeypatch.setattr(status.time, "time", lambda: 100)
    monkeypatch.setenv("SCRUB_EXPIRE", "20")
    monkeypatch.setattr(status, "run", lambda *args: "tank\n" if "list" in args else "SLOW 3")
    monkeypatch.setattr(status, "read_status", lambda *_, **kwargs: {} if kwargs.get("explain") else {"tank": pool})
    assert status.health() == 0


def test_health_reports_failed_spares_and_active_spare_errors(monkeypatch, pool):
    pool["vdevs"]["mirror-0"]["vdevs"] = {"spare0": pool["vdevs"].pop("disk0")}
    pool["vdevs"]["mirror-0"]["vdevs"]["spare0"]["checksum_errors"] = 5
    pool["spares"] = {"spare0": {"state": "INUSE"}, "spare1": {"state": "FAULTED"}}
    monkeypatch.setattr(status.os, "geteuid", lambda: 0)
    monkeypatch.setattr(status.time, "time", lambda: 100)
    monkeypatch.setenv("SCRUB_EXPIRE", "20")
    monkeypatch.setattr(status, "run", lambda *args: "tank\n" if "list" in args else "report")
    monkeypatch.setattr(status, "read_status", lambda *_, **kwargs: {} if kwargs.get("explain") else {"tank": pool})
    mail = []
    monkeypatch.setattr(status.subprocess, "run", lambda *args, **kwargs: mail.append(kwargs["input"]))
    assert status.health() == 1
    assert "Detected drive errors (READ/WRITE/CKSUM) on tank" in mail[0]
    assert "Unhealthy spare spare1 on tank: FAULTED" in mail[0]
    assert "Unhealthy spare spare0" not in mail[0]


def test_bad_pool_does_not_hide_another_pools_errors_and_age(monkeypatch, pool):
    pool["vdevs"]["disk0"]["checksum_errors"] = 5
    pool["scan_stats"]["end_time"] = 70
    bad = copy.deepcopy(pool)
    bad["name"] = "broken"
    bad["vdevs"]["disk0"]["read_errors"] = "bad counter"
    monkeypatch.setattr(status.os, "geteuid", lambda: 0)
    monkeypatch.setattr(status.time, "time", lambda: 100)
    monkeypatch.setenv("SCRUB_EXPIRE", "20")

    def run(*args):
        if "list" in args:
            return "broken\ntank\n"
        if "-j" in args:
            name = args[-1]
            return document({name: bad if name == "broken" else pool})
        return "report"

    monkeypatch.setattr(status, "run", run)
    mail = []
    monkeypatch.setattr(status.subprocess, "run", lambda *args, **kwargs: mail.append(kwargs["input"]))
    assert status.health() == 1
    assert "Cannot query drive errors and scrub age for broken" in mail[0]
    assert "Detected drive errors (READ/WRITE/CKSUM) on tank" in mail[0]
    assert "Scrub expired on tank" in mail[0]
