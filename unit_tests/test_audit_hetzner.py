import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).parents[1] / "mise-tasks" / "ci" / "audit-hetzner.py"
_SPEC = importlib.util.spec_from_file_location("audit_hetzner", _MODULE_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
audit_hetzner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit_hetzner)


def test_preflight_returns_authenticated_server_query(monkeypatch) -> None:
    calls = []
    servers = [{"name": "fox"}]
    monkeypatch.setattr(audit_hetzner.shutil, "which", lambda name: "/usr/bin/hcloud")
    monkeypatch.setattr(audit_hetzner, "hcloud_list", lambda resource: calls.append(resource) or servers)

    assert audit_hetzner.preflight() == servers
    assert calls == ["server"]


def test_anomaly_without_cleanup_has_no_cleanup_section(monkeypatch, capsys) -> None:
    monkeypatch.setattr(audit_hetzner, "anomalies", ["server leak"])
    monkeypatch.setattr(audit_hetzner, "deletes", [])
    monkeypatch.setattr(audit_hetzner, "expected", [])
    monkeypatch.setattr(audit_hetzner, "errors", [])
    monkeypatch.setattr(audit_hetzner, "preflight", list)
    monkeypatch.setattr(audit_hetzner, "safe", lambda label, fn: [])

    with pytest.raises(SystemExit, match="1"):
        audit_hetzner.main()

    output = capsys.readouterr().out
    assert "server leak" in output
    assert "Suggested cleanup" not in output
