"""Check that GUI YAML uploads preserve the Ansible-owned config directory."""

import pytest
from conftest import load_repo_module


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
