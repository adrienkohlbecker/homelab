"""Tests for machine.is_service_role.

The function reads `roles/<role>/tasks/_test.yml` relative to CWD, so each
test stages a tiny role tree under tmp_path and chdirs into it.
"""

from pathlib import Path

import pytest

import machine


def _stage_role(root: Path, role: str, body: str | None) -> None:
    role_dir = root / "roles" / role / "tasks"
    role_dir.mkdir(parents=True)
    if body is not None:
        (role_dir / "_test.yml").write_text(body)


def test_returns_true_when_test_yml_imports_podman(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage_role(
        tmp_path,
        "myservice",
        "- import_role:\n    name: _test\n    tasks_from: podman\n",
    )
    monkeypatch.chdir(tmp_path)
    assert machine.is_service_role("myservice") is True


def test_returns_true_when_test_yml_imports_nginx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage_role(
        tmp_path,
        "edge",
        "- import_role:\n    name: _test\n    tasks_from: nginx\n",
    )
    monkeypatch.chdir(tmp_path)
    assert machine.is_service_role("edge") is True


def test_returns_false_when_test_yml_imports_neither(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage_role(
        tmp_path,
        "ordinary",
        "- import_role:\n    name: _test\n    tasks_from: filesystem\n",
    )
    monkeypatch.chdir(tmp_path)
    assert machine.is_service_role("ordinary") is False


def test_returns_false_when_test_yml_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage_role(tmp_path, "no_test", body=None)
    monkeypatch.chdir(tmp_path)
    assert machine.is_service_role("no_test") is False


def test_returns_false_when_role_directory_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert machine.is_service_role("does_not_exist") is False
