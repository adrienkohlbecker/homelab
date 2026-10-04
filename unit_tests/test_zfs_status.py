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


def test_run_pins_the_c_locale(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="zfs-2.4.1\n")

    monkeypatch.setattr(status.subprocess, "run", run)
    assert status.run("zpool", "--version") == "zfs-2.4.1\n"
    assert calls[0][0] == ["timeout", "-k", "10", "60", "zpool", "--version"]
    assert calls[0][1]["env"]["LC_ALL"] == "C"


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


@pytest.mark.parametrize("state", ["OFFLINE", "REMOVED", "SPLIT", "UNKNOWN", pytest.param("DÉGRADÉ", id="translated")])
def test_unconsumed_pool_fields_are_left_to_zpool_x(pool, state):
    pool["state"] = state
    del pool["error_count"]
    del pool["scan_stats"]["start_time"]
    pool["vdevs"]["disk0"]["slow_ios"] = "unconsumed"
    assert status.parse_status(document({"tank": pool}))["tank"]["state"] == state


def test_one_unusual_pool_does_not_blank_the_host_scan(monkeypatch, pool):
    removed = copy.deepcopy(pool) | {"name": "removed", "state": "REMOVED"}
    pool["scan_stats"].update(state="SCANNING")
    monkeypatch.setattr(status, "supports_json", lambda: True)
    monkeypatch.setattr(status, "run", lambda *_: document({"removed": removed, "tank": pool}))
    assert status.scan_in_progress(None)


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
        pytest.param("SCRUB", "SCANNING", 0, True, True, id="active_scrub"),
        pytest.param("SCRUB", "SCANNING", 95, False, True, id="paused_scrub"),
        pytest.param("SCRUB", "CANCELED", 95, False, False, id="canceled_scrub"),
        pytest.param("SCRUB", "FINISHED", 0, False, False, id="finished_scrub"),
        pytest.param("RESILVER", "SCANNING", 0, True, False, id="active_resilver"),
        pytest.param("RESILVER", "SCANNING", 0, False, True, id="resilver_is_not_a_scrub"),
        pytest.param("RESILVER", "FINISHED", 0, False, False, id="finished_resilver"),
    ],
)
def test_scan_activity_distinguishes_pause_and_history(pool, function, state, pause, active, scrub_only):
    pool["scan_stats"].update(function=function, state=state, scrub_pause=pause)
    assert status.scan_active(pool, scrub_only=scrub_only) is active


@pytest.mark.parametrize(
    ("err_state", "err_pause", "active", "scrub_only"),
    [
        pytest.param("ERRORSCRUBBING", 0, True, False, id="active_error_scrub"),
        pytest.param("ERRORSCRUBBING", 0, False, True, id="error_scrub_not_pausable"),
        pytest.param("ERRORSCRUBBING", 95, False, False, id="paused_error_scrub"),
        pytest.param("FINISHED", 0, False, False, id="finished_error_scrub"),
    ],
)
def test_error_scrub_activity_comes_from_its_own_fields(pool, err_state, err_pause, active, scrub_only):
    pool["scan_stats"].update(err_scrub_func="ERRORSCRUB", err_scrub_state=err_state, err_scrub_pause=err_pause)
    parsed = status.parse_status(document({"tank": pool}))["tank"]
    assert status.scan_active(parsed, scrub_only=scrub_only) is active


@pytest.mark.parametrize(("field", "value"), [("function", "ERRORSCRUB"), ("state", "ERRORSCRUBBING")])
def test_error_scrub_never_appears_in_the_main_scan_fields(pool, field, value):
    pool["scan_stats"][field] = value
    with pytest.raises(status.StatusError):
        status.parse_status(document({"tank": pool}))


@pytest.mark.parametrize(("field", "value"), [("err_scrub_state", "unexpected"), ("err_scrub_pause", "0")])
def test_rejects_unknown_or_incomplete_error_scrub(pool, field, value):
    pool["scan_stats"].update(err_scrub_func="ERRORSCRUB", err_scrub_state="ERRORSCRUBBING", err_scrub_pause=0)
    pool["scan_stats"][field] = value
    with pytest.raises(status.StatusError, match="err_scrub"):
        status.parse_status(document({"tank": pool}))


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
    monkeypatch.setattr(status.sys, "argv", ["zfs_health", "health"])
    monkeypatch.setattr(status, "supports_json", lambda: json_supported)
    calls = []
    monkeypatch.setattr(status, "health", lambda expire: calls.append(("json", expire)) or 0)

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs == {"check": False}
        return subprocess.CompletedProcess(argv, 7)

    monkeypatch.setattr(status.subprocess, "run", run)
    assert status.main() == (0 if json_supported else 7)
    assert calls == ([("json", 3456000)] if json_supported else [["/opt/zfs/zfs_health_legacy.sh"]])


def test_legacy_launch_failure_is_mailed(monkeypatch):
    monkeypatch.setattr(status.sys, "argv", ["zfs_health", "health"])
    monkeypatch.setattr(status, "supports_json", lambda: False)
    mail = []

    def run(argv, **kwargs):
        if argv[0] == "/opt/zfs/zfs_health_legacy.sh":
            raise OSError("Exec format error")
        mail.append(kwargs["input"])

    monkeypatch.setattr(status.subprocess, "run", run)
    assert status.main() == 1
    assert len(mail) == 1
    assert "Cannot initialize health check: Exec format error" in mail[0]


def test_unknown_version_fails_closed(monkeypatch):
    monkeypatch.setattr(status, "run", lambda *_: "unexpected")
    with pytest.raises(status.StatusError, match="userspace version"):
        status.supports_json()


@pytest.mark.parametrize(
    "error",
    [
        status.StatusError("Cannot identify the ZFS userspace version"),
        subprocess.CalledProcessError(124, ["zpool", "--version"], stderr="version query timed out"),
        OSError("zpool is unavailable"),
    ],
)
def test_health_startup_failures_are_mailed(monkeypatch, error, capsys):
    monkeypatch.setattr(status.sys, "argv", ["zfs_health", "health"])

    def fail():
        raise error

    monkeypatch.setattr(status, "supports_json", fail)
    mail = []
    monkeypatch.setattr(status.subprocess, "run", lambda *args, **kwargs: mail.append((args, kwargs)))
    assert status.main() == 1
    assert "Cannot initialize health check" in capsys.readouterr().err
    assert len(mail) == 1
    assert mail[0][0][0][0] == "mail"
    assert status.diagnostic(error) in mail[0][1]["input"]


@pytest.mark.parametrize("expire", ["invalid", "", "-1"])
def test_invalid_expiration_is_mailed_before_selecting_either_parser(monkeypatch, expire):
    monkeypatch.setattr(status.sys, "argv", ["zfs_health", "health"])
    monkeypatch.setenv("SCRUB_EXPIRE", expire)

    def unexpected_version_query():
        pytest.fail("Invalid expiration must be reported before running either health parser")

    monkeypatch.setattr(status, "supports_json", unexpected_version_query)
    mail = []
    monkeypatch.setattr(status.subprocess, "run", lambda *args, **kwargs: mail.append(kwargs["input"]))
    assert status.main() == 1
    assert len(mail) == 1
    assert "Cannot initialize health check" in mail[0]


def test_scan_version_failure_does_not_send_a_health_mail(monkeypatch, capsys):
    monkeypatch.setattr(status.sys, "argv", ["zfs_status.py", "scan"])

    def fail():
        raise status.StatusError("Cannot identify the ZFS userspace version")

    monkeypatch.setattr(status, "supports_json", fail)
    monkeypatch.setattr(status.subprocess, "run", lambda *_, **__: pytest.fail("Scan queries must not mail"))
    assert status.main() == 2
    assert "Cannot identify" in capsys.readouterr().err


def test_noble_scan_fallback_does_not_request_json(monkeypatch):
    monkeypatch.setattr(status, "supports_json", lambda: False)
    calls = []

    def run(*argv):
        calls.append(argv)
        return "scan: scrub in progress"

    monkeypatch.setattr(status, "run", run)
    assert status.scan_in_progress("tank")
    assert calls == [("zpool", "status", "tank")]


@pytest.mark.parametrize("scrub_only", [False, True])
@pytest.mark.parametrize(
    ("output", "active", "scrub_active"),
    [
        ("scan: resilver (mirror-0) in progress since Thu Oct 1 00:00:00 2026", True, False),
        ("scan: resilver in progress since Thu Oct 1 00:00:00 2026", True, False),
        ("scan: resilvered (mirror-0) 64M with 0 errors on Thu Oct 1 00:00:00 2026", False, False),
        ("scan: scrub in progress since Thu Oct 1 00:00:00 2026", True, True),
    ],
)
def test_noble_scan_fallback_distinguishes_active_rebuilds(monkeypatch, scrub_only, output, active, scrub_active):
    monkeypatch.setattr(status, "supports_json", lambda: False)
    monkeypatch.setattr(status.subprocess, "run", lambda argv, **_: subprocess.CompletedProcess(argv, 0, stdout=output))
    assert status.scan_in_progress("tank", scrub_only=scrub_only) is (scrub_active if scrub_only else active)


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
        pytest.param("FINISHED", 0, 90, None, id="recent_scrub"),
        pytest.param("FINISHED", 0, 70, "expired", id="expired_scrub"),
        pytest.param("SCANNING", 70, 0, "expired", id="stale_pause"),
        pytest.param("SCANNING", 0, 0, None, id="active_scrub"),
        pytest.param("CANCELED", 95, 0, "canceled", id="canceled_scrub"),
    ],
)
def test_scrub_watchdog_uses_integer_timestamps(pool, state, pause, end, expected):
    pool["scan_stats"].update(state=state, scrub_pause=pause, end_time=end)
    issue = status.scrub_issue("tank", pool, now=100, expire=20)
    assert issue is None if expected is None else expected in issue


@pytest.mark.parametrize("canceled_resilver", [False, True])
def test_creation_baseline_is_explained_without_a_usable_scan(monkeypatch, pool, canceled_resilver):
    if canceled_resilver:
        pool["scan_stats"].update(function="RESILVER", state="CANCELED")
    else:
        pool.pop("scan_stats")
    monkeypatch.setattr(status, "run", lambda *_: "70\n")
    assert status.scrub_issue("tank", pool, now=100, expire=20) == (
        "Scrub expired on tank (age since pool creation; no usable scan timestamp)"
    )


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


@pytest.fixture
def health_env(monkeypatch):
    """Run health() as root at t=100 and collect each mailed body."""
    monkeypatch.setattr(status.os, "geteuid", lambda: 0)
    monkeypatch.setattr(status.time, "time", lambda: 100)
    mail = []

    def send(argv, **kwargs):
        assert argv[0] == "mail"
        mail.append(kwargs["input"])

    monkeypatch.setattr(status.subprocess, "run", send)
    return mail


def test_health_mails_faults_and_includes_slow_io_diagnostics(monkeypatch, pool, health_env, capsys):
    pool["vdevs"]["disk0"]["checksum_errors"] = 5
    healthy = copy.deepcopy(pool)
    healthy["vdevs"]["disk0"]["checksum_errors"] = 0
    monkeypatch.setattr(status, "run", lambda *args: "tank\n" if "list" in args else "SLOW\ndisk0 ONLINE 0 0 5 3")
    monkeypatch.setattr(
        status, "read_status", lambda *_, **kwargs: {"tank": healthy} if kwargs.get("explain") else {"tank": pool}
    )
    assert status.health(20) == 1
    assert "Detected drive errors (READ/WRITE/CKSUM)" in capsys.readouterr().err
    assert len(health_env) == 1
    assert "SLOW" in health_env[0]


def test_health_query_failure_is_mailed(monkeypatch, health_env, capsys):
    monkeypatch.setattr(status, "run", lambda *args: "tank\n" if "list" in args else "report")

    def fail(*_, **__):
        raise status.StatusError("bad JSON schema")

    monkeypatch.setattr(status, "read_status", fail)
    assert status.health(20) == 1
    assert "bad JSON schema" in capsys.readouterr().err
    assert len(health_env) == 1


def test_health_accepts_feature_capped_pools_and_does_not_alarm_on_slow_io(monkeypatch, pool, health_env):
    pool["vdevs"]["disk0"]["slow_ios"] = 3
    monkeypatch.setattr(status, "run", lambda *args: "tank\n" if "list" in args else "SLOW 3")
    monkeypatch.setattr(status, "read_status", lambda *_, **kwargs: {} if kwargs.get("explain") else {"tank": pool})
    assert status.health(20) == 0
    assert health_env == []


def test_health_reports_failed_spares_and_active_spare_errors(monkeypatch, pool, health_env):
    pool["vdevs"]["mirror-0"]["vdevs"] = {"spare0": pool["vdevs"].pop("disk0")}
    pool["vdevs"]["mirror-0"]["vdevs"]["spare0"]["checksum_errors"] = 5
    pool["spares"] = {"spare0": {"state": "INUSE"}, "spare1": {"state": "FAULTED"}, "spare2": {"state": "FUTURE"}}
    monkeypatch.setattr(status, "run", lambda *args: "tank\n" if "list" in args else "report")
    monkeypatch.setattr(status, "read_status", lambda *_, **kwargs: {} if kwargs.get("explain") else {"tank": pool})
    assert status.health(20) == 1
    assert "Detected drive errors (READ/WRITE/CKSUM) on tank" in health_env[0]
    assert "Unhealthy spare spare1 on tank: FAULTED" in health_env[0]
    assert "Unhealthy spare spare2 on tank: FUTURE" in health_env[0]
    assert "Unhealthy spare spare0" not in health_env[0]


def test_bad_pool_does_not_hide_another_pools_errors_and_age(monkeypatch, pool, health_env):
    pool["vdevs"]["disk0"]["checksum_errors"] = 5
    pool["scan_stats"]["end_time"] = 70
    bad = copy.deepcopy(pool)
    bad["name"] = "broken"
    bad["vdevs"]["disk0"]["read_errors"] = "bad counter"

    def run(*args):
        if "list" in args:
            return "broken\ntank\n"
        if "-j" in args:
            name = args[-1]
            return document({name: bad if name == "broken" else pool})
        return "report"

    monkeypatch.setattr(status, "run", run)
    assert status.health(20) == 1
    assert "Cannot query drive errors and scrub age for broken" in health_env[0]
    assert "Detected drive errors (READ/WRITE/CKSUM) on tank" in health_env[0]
    assert "Scrub expired on tank" in health_env[0]
