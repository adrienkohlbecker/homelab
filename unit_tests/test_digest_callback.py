"""Unit tests for callback_plugins/digest.py.

Exercises the pure transformation functions (_is_diff, _digest,
_collapse_large_facts) that drive the digest stdout callback, plus a smoke
test on the CallbackModule plugin metadata. No ansible run is needed - the
functions operate on plain result dicts.
"""

import importlib.util
import copy
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "callback_plugins" / "digest.py"


def _load():
    spec = importlib.util.spec_from_file_location("digest_callback", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


digest = _load()


# A representative full systemd `status` dict (trimmed but with the noise keys
# the callback is meant to drop).
FULL_STATUS = {
    "Id": "nginx.service",
    "ActiveState": "active",
    "SubState": "running",
    "Result": "success",
    "ExecMainPID": "1234",
    "WatchdogUSec": "0",
    "MemoryCurrent": "12345678",
    "ControlGroup": "/system.slice/nginx.service",
    "ActiveEnterTimestamp": "Thu 2026-06-18 21:23:35 UTC",
    "ActiveEnterTimestampMonotonic": "41655907",
}
SUMMARY_STATUS = {
    "Id": "nginx.service",
    "ActiveState": "active",
    "SubState": "running",
    "Result": "success",
    "ExecMainPID": "1234",
}

# A representative full `stat` dict (file exists).
FULL_STAT = {
    "exists": True,
    "path": "/etc/nginx/nginx.conf",
    "mode": "0644",
    "isreg": True,
    "isdir": False,
    "islnk": False,
    "size": 8419,
    "checksum": "abc",
    "pw_name": "root",
    "gr_name": "root",
    "mtime": 1781858272.9,
    "atime": 1781858272.9,
    "ctime": 1781858272.9,
    "uid": 0,
    "gid": 0,
    "dev": 64,
    "inode": 12,
    "nlink": 1,
    "rusr": True,
    "wusr": True,
    "xusr": False,
    "rgrp": True,
}
SUMMARY_STAT = {
    "exists": True,
    "path": "/etc/nginx/nginx.conf",
    "mode": "0644",
    "isreg": True,
    "isdir": False,
    "islnk": False,
    "size": 8419,
    "checksum": "abc",
    "pw_name": "root",
    "gr_name": "root",
    "mtime": 1781858272.9,
}


# ---------------------------------------------------------------------------
# _is_diff
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ({"before": "a\n", "after": "b\n"}, True),
        ({"prepared": "x"}, True),
        ({"after": "only"}, True),
        ([{"before": "a", "after": "b"}], True),
        ([{"before": "a"}, {"after": "b"}], True),
        ([], False),  # empty list is not a diff
        ([{"before": "a"}, {"nope": 1}], False),  # one non-diff item disqualifies
        ({"unrelated": 1}, False),
        ("a string named diff", False),
        (42, False),
    ],
)
def test_is_diff(value, expected):
    assert digest._is_diff(value) is expected


# ---------------------------------------------------------------------------
# _digest - systemd status
# ---------------------------------------------------------------------------


def test_status_collapsed_top_level():
    result = {"changed": True, "name": "nginx", "status": dict(FULL_STATUS)}
    out = digest._digest(result)
    assert out["status"] == SUMMARY_STATUS
    assert out["changed"] is True and out["name"] == "nginx"


def test_status_collapsed_nested_under_facts():
    # The systemd_unit helper persists the registered result via set_fact.
    result = {"ansible_facts": {"nginx_started_result": {"changed": False, "status": dict(FULL_STATUS)}}}
    out = digest._digest(result)
    assert out["ansible_facts"]["nginx_started_result"]["status"] == SUMMARY_STATUS


def test_status_collapsed_in_loop_results():
    result = {"results": [{"status": dict(FULL_STATUS)}, {"status": dict(FULL_STATUS)}]}
    out = digest._digest(result)
    assert all(r["status"] == SUMMARY_STATUS for r in out["results"])


def test_non_systemd_status_untouched():
    # uri module returns an int status; a dict without the ActiveState marker
    # must be left alone.
    assert digest._digest({"status": 200})["status"] == 200
    other = {"status": {"phase": "Running", "ready": True}}
    assert digest._digest(other)["status"] == {"phase": "Running", "ready": True}


def test_status_missing_some_keep_keys():
    result = {"status": {"ActiveState": "failed", "Result": "exit-code"}}
    assert digest._digest(result)["status"] == {"ActiveState": "failed", "Result": "exit-code"}


# ---------------------------------------------------------------------------
# _digest - stat
# ---------------------------------------------------------------------------


def test_stat_collapsed_top_level():
    out = digest._digest({"changed": False, "stat": dict(FULL_STAT)})
    assert out["stat"] == SUMMARY_STAT


def test_stat_absent_kept_minimal():
    out = digest._digest({"stat": {"exists": False}})
    assert out["stat"] == {"exists": False}


def test_stat_collapsed_nested_and_in_loop():
    result = {
        "ansible_facts": {"cert": {"stat": dict(FULL_STAT)}},
        "results": [{"item": "/a", "stat": dict(FULL_STAT)}, {"item": "/b", "stat": {"exists": False}}],
    }
    out = digest._digest(result)
    assert out["ansible_facts"]["cert"]["stat"] == SUMMARY_STAT
    assert out["results"][0]["stat"] == SUMMARY_STAT
    assert out["results"][1]["stat"] == {"exists": False}


def test_non_stat_dict_without_exists_untouched():
    other = {"stat": {"foo": 1, "bar": 2}}
    assert digest._digest(other)["stat"] == {"foo": 1, "bar": 2}


# ---------------------------------------------------------------------------
# _digest - duplicated diff under ansible_facts
# ---------------------------------------------------------------------------


def test_diff_dropped_when_persisted_under_facts():
    result = {
        "ansible_facts": {
            "conf_result": {
                "changed": True,
                "dest": "/etc/x",
                "diff": {"before": "a\n", "after": "b\n", "before_header": "/etc/x"},
            }
        }
    }
    out = digest._digest(result)
    persisted = out["ansible_facts"]["conf_result"]
    assert "diff" not in persisted
    assert persisted["changed"] is True and persisted["dest"] == "/etc/x"


def test_diff_kept_at_top_level():
    # A task's own diff lives at the top of the result (sibling of changed),
    # not under ansible_facts; the callback renders it via its diff path, so
    # the digest must NOT strip it.
    result = {"changed": True, "diff": {"before": "a\n", "after": "b\n"}}
    out = digest._digest(result)
    assert out["diff"] == {"before": "a\n", "after": "b\n"}


def test_non_diff_fact_named_diff_kept():
    # A fact literally named `diff` that isn't a diff structure stays.
    result = {"ansible_facts": {"diff": "just a string"}}
    out = digest._digest(result)
    assert out["ansible_facts"]["diff"] == "just a string"


# ---------------------------------------------------------------------------
# _digest - structural guarantees
# ---------------------------------------------------------------------------


def test_digest_does_not_mutate_input():
    result = {"status": dict(FULL_STATUS), "ansible_facts": {"r": {"stat": dict(FULL_STAT)}}}
    snapshot = copy.deepcopy(result)
    digest._digest(result)
    assert result == snapshot


def test_scalars_and_unknown_passthrough():
    assert digest._digest("x") == "x"
    assert digest._digest(7) == 7
    assert digest._digest(None) is None
    assert digest._digest({"msg": "ok", "rc": 0}) == {"msg": "ok", "rc": 0}


# ---------------------------------------------------------------------------
# _collapse_large_facts
# ---------------------------------------------------------------------------


def test_large_facts_collapsed_to_key_list():
    facts = {f"k{i:02d}": i for i in range(digest._FACTS_DIGEST_THRESHOLD + 1)}
    out = digest._collapse_large_facts({"ansible_facts": dict(facts)})
    assert isinstance(out["ansible_facts"], str)
    assert f"{len(facts)} facts hidden" in out["ansible_facts"]
    # keys listed, sorted
    assert "k00" in out["ansible_facts"] and "k25" in out["ansible_facts"]


def test_small_facts_kept_in_full():
    facts = {"apt_source_present": True, "apt_source_arch": "arm64"}
    out = digest._collapse_large_facts({"ansible_facts": dict(facts)})
    assert out["ansible_facts"] == facts


def test_collapse_ignores_non_dict_result():
    assert digest._collapse_large_facts("x") == "x"
    assert digest._collapse_large_facts({"ansible_facts": "already a string"})["ansible_facts"] == "already a string"


# ---------------------------------------------------------------------------
# CallbackModule plugin metadata
# ---------------------------------------------------------------------------


def test_callback_module_metadata():
    from ansible.plugins.callback.default import CallbackModule as Default

    assert issubclass(digest.CallbackModule, Default)
    assert digest.CallbackModule.CALLBACK_NAME == "digest"
    assert digest.CallbackModule.CALLBACK_TYPE == "stdout"
