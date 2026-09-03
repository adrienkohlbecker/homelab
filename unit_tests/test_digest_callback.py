"""Unit tests for callback_plugins/digest.py."""

import copy
import json
from typing import Any, cast

import callback_plugins.digest as digest

SUMMARY_STATUS = {
    "Id": "nginx.service",
    "ActiveState": "active",
    "SubState": "running",
    "Result": "success",
    "ExecMainPID": "1234",
}
FULL_STATUS = SUMMARY_STATUS | {
    "WatchdogUSec": "0",
    "MemoryCurrent": "12345678",
    "ControlGroup": "/system.slice/nginx.service",
    "ActiveEnterTimestamp": "Thu 2026-06-18 21:23:35 UTC",
    "ActiveEnterTimestampMonotonic": "41655907",
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
FULL_STAT = SUMMARY_STAT | {
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


def display_result(result) -> dict[str, Any]:
    # serialize=False makes _dump_results return the digested dict, not the
    # JSON string its annotation declares.
    return cast("dict[str, Any]", digest.CallbackModule()._dump_results(result, serialize=False))


def test_status_collapsed_top_level():
    result = {"changed": True, "name": "nginx", "status": dict(FULL_STATUS)}
    out = display_result(result)
    assert out["status"] == SUMMARY_STATUS
    assert out["changed"] is True
    assert out["name"] == "nginx"


def test_status_collapsed_nested_under_facts():
    # The systemd_unit helper persists the registered result via set_fact.
    result = {"ansible_facts": {"nginx_started_result": {"changed": False, "status": dict(FULL_STATUS)}}}
    out = display_result(result)
    assert out["ansible_facts"]["nginx_started_result"]["status"] == SUMMARY_STATUS


def test_status_collapsed_in_loop_results():
    result = {"results": [{"status": dict(FULL_STATUS)}, {"status": dict(FULL_STATUS)}]}
    out = display_result(result)
    assert all(r["status"] == SUMMARY_STATUS for r in out["results"])


def test_non_systemd_status_untouched():
    # uri module returns an int status; a dict without the ActiveState marker
    # must be left alone.
    assert display_result({"status": 200})["status"] == 200
    other = {"status": {"phase": "Running", "ready": True}}
    assert display_result(other)["status"] == {"phase": "Running", "ready": True}


def test_status_missing_some_keep_keys():
    result = {"status": {"ActiveState": "failed", "Result": "exit-code"}}
    assert display_result(result)["status"] == {"ActiveState": "failed", "Result": "exit-code"}


def test_stat_collapsed_top_level():
    out = display_result({"changed": False, "stat": dict(FULL_STAT)})
    assert out["stat"] == SUMMARY_STAT


def test_stat_absent_kept_minimal():
    out = display_result({"stat": {"exists": False}})
    assert out["stat"] == {"exists": False}


def test_stat_collapsed_nested_and_in_loop():
    result = {
        "ansible_facts": {"cert": {"stat": dict(FULL_STAT)}},
        "results": [{"item": "/a", "stat": dict(FULL_STAT)}, {"item": "/b", "stat": {"exists": False}}],
    }
    out = display_result(result)
    assert out["ansible_facts"]["cert"]["stat"] == SUMMARY_STAT
    assert out["results"][0]["stat"] == SUMMARY_STAT
    assert out["results"][1]["stat"] == {"exists": False}


def test_non_stat_dict_without_exists_untouched():
    other = {"stat": {"foo": 1, "bar": 2}}
    assert display_result(other)["stat"] == {"foo": 1, "bar": 2}


def test_large_uri_json_response_collapsed():
    payload = {"alarms": {f"alarm_{i}": {"status": "CLEAR"} for i in range(100)}}
    content = json.dumps(payload)
    result = {
        "changed": False,
        "status": 200,
        "url": "http://localhost:19999/api/v1/alarms?all",
        "json": payload,
        "content": content,
    }
    snapshot = copy.deepcopy(result)

    out = display_result(result)

    assert out["json"].endswith("JSON object with keys: alarms hidden>")
    assert "content" not in out
    assert out["status"] == 200
    assert out["url"] == result["url"]
    assert result == snapshot


def test_small_uri_json_response_keeps_parsed_json():
    payload = {"status": "ok"}
    result = {
        "status": 200,
        "url": "http://localhost:19999/api/v1/info",
        "json": payload,
        "content": '{"status":"ok"}',
    }
    assert display_result(result) == {
        "status": 200,
        "url": "http://localhost:19999/api/v1/info",
        "json": payload,
    }


def test_non_uri_json_result_kept():
    payload = {f"key_{i}": "x" * 100 for i in range(100)}
    assert display_result({"json": payload})["json"] == payload


def test_unserializable_json_value_left_untouched():
    # Mixed-type keys defeat sort_keys serialization inside _json_summary.
    payload = {1: "x" * 3000, "b": "y" * 3000}
    result = {
        "status": 200,
        "url": "http://localhost:19999/api/v1/alarms?all",
        "json": payload,
        "content": "not json",
    }
    assert display_result(result)["json"] == payload


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
    out = display_result(result)
    persisted = out["ansible_facts"]["conf_result"]
    assert "diff" not in persisted
    assert "diff" not in out["ansible_facts"]["loop_result"]
    assert persisted["changed"] is True
    assert persisted["dest"] == "/etc/x"


def test_default_callback_drops_top_level_diff_from_result_dump():
    result = {"changed": True, "diff": {"before": "a\n", "after": "b\n"}}
    assert "diff" not in display_result(result)


def test_non_diff_fact_named_diff_kept():
    # A fact literally named `diff` that isn't a diff structure stays.
    result = {"ansible_facts": {"diff": "just a string", "empty_diff": {"diff": []}}}
    out = display_result(result)
    assert out["ansible_facts"]["diff"] == "just a string"
    assert out["ansible_facts"]["empty_diff"]["diff"] == []


def test_digest_does_not_mutate_input():
    result = {"status": dict(FULL_STATUS), "ansible_facts": {"r": {"stat": dict(FULL_STAT)}}}
    snapshot = copy.deepcopy(result)
    display_result(result)
    assert result == snapshot


def test_unknown_result_keys_passthrough():
    assert display_result({"msg": "ok", "rc": 0}) == {"msg": "ok", "rc": 0}


def test_large_facts_collapsed_to_key_list():
    facts = {f"k{i:02d}": i for i in range(digest._FACTS_DIGEST_THRESHOLD + 1)}
    out = display_result({"ansible_facts": dict(facts)})
    assert isinstance(out["ansible_facts"], str)
    assert f"{len(facts)} facts hidden" in out["ansible_facts"]
    # keys listed, sorted
    assert "k00" in out["ansible_facts"]
    assert "k25" in out["ansible_facts"]


def test_nested_large_facts_collapsed_to_key_list():
    facts = {f"k{i:02d}": i for i in range(digest._FACTS_DIGEST_THRESHOLD + 1)}
    out = display_result({"results": [{"ansible_facts": facts}]})
    assert f"{len(facts)} facts hidden" in out["results"][0]["ansible_facts"]


def test_small_facts_kept_in_full():
    facts = {"apt_source_present": True, "apt_source_arch": "arm64"}
    out = display_result({"ansible_facts": dict(facts)})
    assert out["ansible_facts"] == facts


def test_string_facts_kept():
    assert display_result({"ansible_facts": "already a string"})["ansible_facts"] == "already a string"


def test_callback_module_metadata():
    from ansible.plugins.callback.default import CallbackModule as Default

    assert issubclass(digest.CallbackModule, Default)
    assert digest.CallbackModule.CALLBACK_NAME == "digest"
    assert digest.CallbackModule.CALLBACK_TYPE == "stdout"
