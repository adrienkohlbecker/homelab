"""Unit tests for the scheduled pipeline full-universe status guard."""

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "mise-tasks" / "ci" / "schedule-guard.py"


def _load():
    spec = importlib.util.spec_from_file_location("schedule_guard", _MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


schedule_guard = _load()


def _mock_child_api(*, child_status="success", jobs=None):
    child_jobs = jobs or [
        {"id": 1, "name": "role:box", "status": "success", "allow_failure": False},
        {
            "id": 2,
            "name": "_site_test:box",
            "status": "success",
            "allow_failure": False,
        },
    ]

    def get_all(url, token, token_kind):
        if url.endswith("/pipelines/10/bridges"):
            return [
                {
                    "name": "test_cells",
                    "downstream_pipeline": {"id": 20, "status": child_status},
                }
            ]
        if url.endswith("/pipelines/20/jobs"):
            return child_jobs
        return []

    return get_all


@pytest.fixture(autouse=True)
def full_universe_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        schedule_guard,
        "_full_universe_job_names",
        lambda: {"role:box", "_site_test:box"},
    )


class TestFullUniverseChild:
    def test_accepts_green_full_universe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(schedule_guard, "_gl_api_get_all", _mock_child_api())

        assert schedule_guard._full_universe_child("http://api", 10, "token", "job") == {
            "id": 20,
            "status": "success",
        }

    def test_rejects_green_partial_matrix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            schedule_guard,
            "_gl_api_get_all",
            _mock_child_api(jobs=[{"id": 1, "name": "role:box", "status": "success"}]),
        )

        assert schedule_guard._full_universe_child("http://api", 10, "token", "job") is None

    def test_accepts_failed_full_universe_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        jobs = [
            {"id": 1, "name": "role:box", "status": "failed", "allow_failure": False},
            {
                "id": 2,
                "name": "_site_test:box",
                "status": "success",
                "allow_failure": False,
            },
        ]
        monkeypatch.setattr(
            schedule_guard,
            "_gl_api_get_all",
            _mock_child_api(child_status="failed", jobs=jobs),
        )

        assert schedule_guard._full_universe_child("http://api", 10, "token", "job") == {
            "id": 20,
            "status": "failed",
        }

    def test_rejects_optional_manual_universe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        jobs = [
            {"id": 1, "name": "role:box", "status": "manual", "allow_failure": True},
            {
                "id": 2,
                "name": "_site_test:box",
                "status": "manual",
                "allow_failure": True,
            },
        ]
        monkeypatch.setattr(schedule_guard, "_gl_api_get_all", _mock_child_api(jobs=jobs))

        assert schedule_guard._full_universe_child("http://api", 10, "token", "job") is None


class TestLatestFullTestStatus:
    def test_manual_parent_with_green_full_universe_is_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pipelines = [
            {
                "id": 10,
                "source": "push",
                "status": "manual",
                "sha": "new",
                "created_at": "2026-01-02",
            },
            {
                "id": 9,
                "source": "push",
                "status": "failed",
                "sha": "old",
                "created_at": "2026-01-01",
            },
        ]
        monkeypatch.setattr(schedule_guard, "_gl_api_get", lambda *args, **kwargs: pipelines)
        monkeypatch.setattr(
            schedule_guard,
            "_full_universe_child",
            lambda project_api, pipeline_id, token, token_kind: {
                "id": 20,
                "status": "success",
            },
        )

        result = schedule_guard.latest_full_test_status(
            "master",
            project_api="http://api",
            token="token",
            token_kind="job",
            exclude_id=99,
            log_fn=lambda message: None,
        )

        assert result is not None
        assert result["id"] == 10
        assert result["status"] == "success"
        assert result["parent_status"] == "manual"
        assert result["child_pipeline_id"] == 20

    def test_partial_newer_pipeline_does_not_hide_failed_full_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pipelines = [
            {
                "id": 10,
                "source": "push",
                "status": "manual",
                "sha": "new",
                "created_at": "2026-01-02",
            },
            {
                "id": 9,
                "source": "push",
                "status": "failed",
                "sha": "old",
                "created_at": "2026-01-01",
            },
        ]
        monkeypatch.setattr(schedule_guard, "_gl_api_get", lambda *args, **kwargs: pipelines)
        children = {10: None, 9: {"id": 19, "status": "failed"}}
        monkeypatch.setattr(
            schedule_guard,
            "_full_universe_child",
            lambda project_api, pipeline_id, token, token_kind: children[pipeline_id],
        )

        result = schedule_guard.latest_full_test_status(
            "master",
            project_api="http://api",
            token="token",
            token_kind="job",
            exclude_id=99,
            log_fn=lambda message: None,
        )

        assert result is not None
        assert result["id"] == 9
        assert result["status"] == "failed"

    def test_failed_parent_stays_failed_when_child_is_green(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pipelines = [
            {
                "id": 10,
                "source": "push",
                "status": "failed",
                "sha": "new",
                "created_at": "2026-01-02",
            }
        ]
        monkeypatch.setattr(schedule_guard, "_gl_api_get", lambda *args, **kwargs: pipelines)
        monkeypatch.setattr(
            schedule_guard,
            "_full_universe_child",
            lambda project_api, pipeline_id, token, token_kind: {
                "id": 20,
                "status": "success",
            },
        )

        result = schedule_guard.latest_full_test_status(
            "master",
            project_api="http://api",
            token="token",
            token_kind="job",
            exclude_id=99,
            log_fn=lambda message: None,
        )

        assert result is not None
        assert result["status"] == "failed"
