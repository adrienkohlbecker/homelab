"""Unit tests for callback_plugins/digest.py."""

import importlib.util
import copy
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "callback_plugins" / "digest.py"


def _load():
    spec = importlib.util.spec_from_file_location("digest_callback", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


digest = _load()


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


def test_status_collapsed_top_level():
    result = {"changed": True, "name": "nginx", "status": dict(FULL_STATUS)}
    out = digest._digest_result(result)
    assert out["status"] == SUMMARY_STATUS
    assert out["changed"] is True and out["name"] == "nginx"


def test_status_collapsed_nested_under_facts():
    # The systemd_unit helper persists the registered result via set_fact.
    result = {"ansible_facts": {"nginx_started_result": {"changed": False, "status": dict(FULL_STATUS)}}}
    out = digest._digest_result(result)
    assert out["ansible_facts"]["nginx_started_result"]["status"] == SUMMARY_STATUS


def test_status_collapsed_in_loop_results():
    result = {"results": [{"status": dict(FULL_STATUS)}, {"status": dict(FULL_STATUS)}]}
    out = digest._digest_result(result)
    assert all(r["status"] == SUMMARY_STATUS for r in out["results"])


def test_non_systemd_status_untouched():
    # uri module returns an int status; a dict without the ActiveState marker
    # must be left alone.
    assert digest._digest_result({"status": 200})["status"] == 200
    other = {"status": {"phase": "Running", "ready": True}}
    assert digest._digest_result(other)["status"] == {"phase": "Running", "ready": True}


def test_status_missing_some_keep_keys():
    result = {"status": {"ActiveState": "failed", "Result": "exit-code"}}
    assert digest._digest_result(result)["status"] == {"ActiveState": "failed", "Result": "exit-code"}


def test_stat_collapsed_top_level():
    out = digest._digest_result({"changed": False, "stat": dict(FULL_STAT)})
    assert out["stat"] == SUMMARY_STAT


def test_stat_absent_kept_minimal():
    out = digest._digest_result({"stat": {"exists": False}})
    assert out["stat"] == {"exists": False}


def test_stat_collapsed_nested_and_in_loop():
    result = {
        "ansible_facts": {"cert": {"stat": dict(FULL_STAT)}},
        "results": [{"item": "/a", "stat": dict(FULL_STAT)}, {"item": "/b", "stat": {"exists": False}}],
    }
    out = digest._digest_result(result)
    assert out["ansible_facts"]["cert"]["stat"] == SUMMARY_STAT
    assert out["results"][0]["stat"] == SUMMARY_STAT
    assert out["results"][1]["stat"] == {"exists": False}


def test_non_stat_dict_without_exists_untouched():
    other = {"stat": {"foo": 1, "bar": 2}}
    assert digest._digest_result(other)["stat"] == {"foo": 1, "bar": 2}


def test_diff_dropped_when_persisted_under_facts():
    result = {
        "ansible_facts": {
            "conf_result": {
                "changed": True,
                "dest": "/etc/x",
                "diff": {"before": "a\n", "after": "b\n", "before_header": "/etc/x"},
            },
            "loop_result": {"diff": [{"before": "a\n"}, {"after": "b\n"}]},
        }
    }
    out = digest._digest_result(result)
    persisted = out["ansible_facts"]["conf_result"]
    assert "diff" not in persisted
    assert "diff" not in out["ansible_facts"]["loop_result"]
    assert persisted["changed"] is True and persisted["dest"] == "/etc/x"


def test_diff_kept_at_top_level():
    # A task's own diff lives at the top of the result (sibling of changed),
    # not under ansible_facts; the callback renders it via its diff path, so
    # the digest must NOT strip it.
    result = {"changed": True, "diff": {"before": "a\n", "after": "b\n"}}
    out = digest._digest_result(result)
    assert out["diff"] == {"before": "a\n", "after": "b\n"}


def test_non_diff_fact_named_diff_kept():
    # A fact literally named `diff` that isn't a diff structure stays.
    result = {"ansible_facts": {"diff": "just a string", "empty_diff": {"diff": []}}}
    out = digest._digest_result(result)
    assert out["ansible_facts"]["diff"] == "just a string"
    assert out["ansible_facts"]["empty_diff"]["diff"] == []


def test_digest_does_not_mutate_input():
    result = {"status": dict(FULL_STATUS), "ansible_facts": {"r": {"stat": dict(FULL_STAT)}}}
    snapshot = copy.deepcopy(result)
    digest._digest_result(result)
    assert result == snapshot


def test_scalars_and_unknown_passthrough():
    assert digest._digest_result("x") == "x"
    assert digest._digest_result(7) == 7
    assert digest._digest_result(None) is None
    assert digest._digest_result({"msg": "ok", "rc": 0}) == {"msg": "ok", "rc": 0}


def test_large_facts_collapsed_to_key_list():
    facts = {f"k{i:02d}": i for i in range(digest._FACTS_DIGEST_THRESHOLD + 1)}
    out = digest._digest_result({"ansible_facts": dict(facts)})
    assert isinstance(out["ansible_facts"], str)
    assert f"{len(facts)} facts hidden" in out["ansible_facts"]
    # keys listed, sorted
    assert "k00" in out["ansible_facts"] and "k25" in out["ansible_facts"]


def test_small_facts_kept_in_full():
    facts = {"apt_source_present": True, "apt_source_arch": "arm64"}
    out = digest._digest_result({"ansible_facts": dict(facts)})
    assert out["ansible_facts"] == facts


def test_string_facts_kept():
    assert digest._digest_result({"ansible_facts": "already a string"})["ansible_facts"] == "already a string"


def test_callback_module_metadata():
    from ansible.plugins.callback.default import CallbackModule as Default

    assert issubclass(digest.CallbackModule, Default)
    assert digest.CallbackModule.CALLBACK_NAME == "digest"
    assert digest.CallbackModule.CALLBACK_TYPE == "stdout"
