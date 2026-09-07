"""Unit tests for roles/homeassistant/files/smtp_config_entry.py.

Exercises the registry reconciliation against a temporary
.storage/core.config_entries without a running Home Assistant.
"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "roles" / "homeassistant" / "files" / "smtp_config_entry.py"


def _load():
    spec = importlib.util.spec_from_file_location("smtp_config_entry", _MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sce = _load()


def _args(**overrides: Any) -> SimpleNamespace:
    defaults = {
        "name": "email",
        "server": "smtp.example.org",
        "port": 587,
        "encryption": "starttls",
        "sender": "ha@example.org",
        "sender_name": "Home Assistant",
        "username": "ha@example.org",
        "recipient": ["ops@example.org"],
        "timeout": 5,
    }
    return SimpleNamespace(**(defaults | overrides))


def _registry(*entries: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": sce.STORAGE_VERSION,
        "minor_version": sce.STORAGE_MINOR_VERSION,
        "key": "core.config_entries",
        "data": {"entries": list(entries)},
    }


def test_creates_the_entry_when_absent() -> None:
    registry = _registry()
    assert sce.reconcile(registry, sce.build_entry(_args(), "hunter2")) is True

    (entry,) = registry["data"]["entries"]
    assert entry["domain"] == "smtp"
    assert entry["title"] == "email"
    assert entry["data"]["password"] == "hunter2"
    assert entry["data"]["server"] == "smtp.example.org"
    assert [s["unique_id"] for s in entry["subentries"]] == ["ops@example.org"]


def test_is_idempotent() -> None:
    registry = _registry()
    sce.reconcile(registry, sce.build_entry(_args(), "hunter2"))
    before = json.dumps(registry)

    assert sce.reconcile(registry, sce.build_entry(_args(), "hunter2")) is False
    assert json.dumps(registry) == before


def test_rotating_the_password_updates_in_place() -> None:
    registry = _registry()
    sce.reconcile(registry, sce.build_entry(_args(), "hunter2"))
    entry_id = registry["data"]["entries"][0]["entry_id"]

    assert sce.reconcile(registry, sce.build_entry(_args(), "rotated")) is True

    (entry,) = registry["data"]["entries"]
    assert entry["data"]["password"] == "rotated"
    # The entity registry keys entities on the entry id; rotating a credential
    # must not orphan them.
    assert entry["entry_id"] == entry_id


def test_preserves_ha_owned_bookkeeping_and_subentry_ids() -> None:
    registry = _registry()
    sce.reconcile(registry, sce.build_entry(_args(), "hunter2"))
    entry = registry["data"]["entries"][0]
    entry["created_at"] = "2020-01-01T00:00:00+00:00"
    entry["discovery_keys"] = {"zeroconf": ["seen"]}
    entry["subentries"][0]["subentry_id"] = "ha-minted-id"

    assert sce.reconcile(registry, sce.build_entry(_args(server="relay.example.org"), "hunter2")) is True

    entry = registry["data"]["entries"][0]
    assert entry["data"]["server"] == "relay.example.org"
    assert entry["created_at"] == "2020-01-01T00:00:00+00:00"
    assert entry["discovery_keys"] == {"zeroconf": ["seen"]}
    assert entry["subentries"][0]["subentry_id"] == "ha-minted-id"


def test_adopts_a_yaml_imported_entry_without_rewriting_it() -> None:
    registry = _registry()
    sce.reconcile(registry, sce.build_entry(_args(), "hunter2"))
    # What HA leaves behind after migrating the old `notify: platform: smtp`.
    registry["data"]["entries"][0]["source"] = "import"

    assert sce.reconcile(registry, sce.build_entry(_args(), "hunter2")) is False
    assert registry["data"]["entries"][0]["source"] == "import"


def test_leaves_other_integrations_alone() -> None:
    other = {"domain": "mqtt", "entry_id": "keepme", "subentries": []}
    registry = _registry(other)

    sce.reconcile(registry, sce.build_entry(_args(), "hunter2"))

    assert registry["data"]["entries"][0] is other
    assert len(registry["data"]["entries"]) == 2


def test_entry_ids_are_derived_not_random() -> None:
    first, second = _registry(), _registry()
    sce.reconcile(first, sce.build_entry(_args(), "hunter2"))
    sce.reconcile(second, sce.build_entry(_args(), "hunter2"))

    assert first["data"]["entries"][0]["entry_id"] == second["data"]["entries"][0]["entry_id"]


def test_missing_registry_starts_from_an_empty_one(tmp_path: Path) -> None:
    registry = sce.load_registry(tmp_path / "core.config_entries")

    assert registry["data"]["entries"] == []
    assert registry["minor_version"] == sce.STORAGE_MINOR_VERSION


def test_refuses_a_registry_newer_than_the_pinned_shape(tmp_path: Path) -> None:
    path = tmp_path / "core.config_entries"
    newer = _registry()
    newer["minor_version"] = sce.STORAGE_MINOR_VERSION + 1
    path.write_text(json.dumps(newer))

    with pytest.raises(SystemExit, match="newer than"):
        sce.load_registry(path)
