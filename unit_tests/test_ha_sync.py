"""Check GUI YAML sync deployment and credential handling."""

import subprocess

import pytest
from conftest import load_repo_module


def test_plants_require_home_assistant_restart() -> None:
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_plants_test")

    assert ("plants.yaml", "homeassistant.restart", True) in sync.SYNC_SPEC


def test_upload_creates_only_nested_parent_directories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HA_API_TOKEN", raising=False)
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_upload_test")
    commands: list[list[str]] = []
    monkeypatch.setattr(sync, "sh", commands.append)

    sync.upload_to_host(
        [
            sync.SyncFile("automations.yaml", "automation.reload", True),
            sync.SyncFile("dashboards/climate.yaml", None, False),
        ]
    )

    assert len(commands) == 4
    top_level = commands[1][2]
    nested = commands[3][2]
    assert "install -d" not in top_level
    assert "-m 0644 -b" in top_level
    assert nested.startswith(
        "sudo install -d -o homeassistant -g homeassistant -m 0755 /mnt/services/homeassistant/dashboards && "
    )
    assert "-m 0644 -b" in nested


def test_token_reads_keychain_only_when_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_keychain_test")
    monkeypatch.delenv("HA_API_TOKEN", raising=False)
    monkeypatch.setattr(sync.sys, "platform", "darwin")
    commands: list[list[str]] = []

    def keychain(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "example-token\n", "")

    monkeypatch.setattr(sync.subprocess, "run", keychain)
    assert sync.ha_api_token() == "example-token"
    assert commands == [
        ["/usr/bin/security", "find-generic-password", "-a", sync.getpass.getuser(), "-s", sync.KEYCHAIN_SERVICE, "-w"]
    ]


def test_unresolved_op_reference_falls_back_to_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_op_ref_test")
    monkeypatch.setenv("HA_API_TOKEN", "op://example")
    monkeypatch.setattr(sync.sys, "platform", "darwin")
    monkeypatch.setattr(
        sync.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, "keychain-token\n", ""),
    )
    assert sync.ha_api_token() == "keychain-token"


def test_push_retries_reload_after_upload_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_retry_test")
    file = sync.SyncFile("scripts.yaml", "script.reload", True)
    host_bytes = [b"old", b"new"]
    uploads: list[list[object]] = []
    reloads: list[tuple[str, str]] = []
    tags: list[str] = []
    monkeypatch.setattr(sync, "assert_clone_present", lambda: None)
    monkeypatch.setattr(sync, "sh", lambda *args, **kwargs: None)
    monkeypatch.setattr(sync, "commit_and_push", lambda message: False)
    monkeypatch.setattr(sync, "resolve_ref", lambda ref: "old" if ref != "HEAD" else "new")
    monkeypatch.setattr(sync, "enumerate_files", lambda: [file])
    monkeypatch.setattr(sync, "blob_at", lambda ref, rel: b"old" if ref == sync.SYNCED_TAG else b"new")
    monkeypatch.setattr(sync, "host_file", lambda rel: host_bytes[0])
    monkeypatch.setattr(sync, "show_push_diff", lambda changed: None)
    monkeypatch.setattr(sync, "validate_syntax", lambda paths: None)
    monkeypatch.setattr(sync, "ha_api_token", lambda: "example-token")
    monkeypatch.setattr(sync, "upload_to_host", lambda files: uploads.append(files))
    monkeypatch.setattr(sync, "advance_synced_tag", lambda: tags.append("advanced"))

    def reload(service: str, token: str) -> None:
        reloads.append((service, token))
        if len(reloads) == 1:
            raise RuntimeError("reload failed")

    monkeypatch.setattr(sync, "_ha_post", reload)
    with pytest.raises(RuntimeError, match="reload failed"):
        sync.do_push()
    assert uploads == [[file]]
    assert tags == []

    host_bytes[0] = b"new"
    sync.do_push()
    assert uploads == [[file]]
    assert reloads == [("script.reload", "example-token"), ("script.reload", "example-token")]
    assert tags == ["advanced"]


def test_push_without_token_never_uploads(monkeypatch: pytest.MonkeyPatch) -> None:
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_missing_token_test")
    file = sync.SyncFile("scripts.yaml", "script.reload", True)
    monkeypatch.setattr(sync, "assert_clone_present", lambda: None)
    monkeypatch.setattr(sync, "sh", lambda *args, **kwargs: None)
    monkeypatch.setattr(sync, "commit_and_push", lambda message: False)
    monkeypatch.setattr(sync, "resolve_ref", lambda ref: "old")
    monkeypatch.setattr(sync, "enumerate_files", lambda: [file])
    monkeypatch.setattr(sync, "blob_at", lambda ref, rel: b"old" if ref == sync.SYNCED_TAG else b"new")
    monkeypatch.setattr(sync, "host_file", lambda rel: b"old")
    monkeypatch.setattr(sync, "show_push_diff", lambda changed: None)
    monkeypatch.setattr(sync, "validate_syntax", lambda paths: None)
    monkeypatch.setattr(sync, "ha_api_token", lambda: (_ for _ in ()).throw(SystemExit("no token")))
    monkeypatch.setattr(sync, "upload_to_host", lambda files: pytest.fail("uploaded without token"))
    with pytest.raises(SystemExit, match="no token"):
        sync.do_push()


def test_pull_refuses_to_erase_pending_push(monkeypatch: pytest.MonkeyPatch) -> None:
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_pull_pending_test")
    file = sync.SyncFile("scripts.yaml", "script.reload", True)
    monkeypatch.setattr(sync, "assert_clone_present", lambda: None)
    monkeypatch.setattr(sync, "assert_clean_working_tree", lambda: None)
    monkeypatch.setattr(sync, "sh", lambda *args, **kwargs: None)
    monkeypatch.setattr(sync, "resolve_ref", lambda ref: "old")
    monkeypatch.setattr(sync, "enumerate_files", lambda: [file])
    monkeypatch.setattr(sync, "blob_at", lambda ref, rel: b"old" if ref == sync.SYNCED_TAG else b"new")
    with pytest.raises(SystemExit, match="clone changes await push"):
        sync.do_pull()
