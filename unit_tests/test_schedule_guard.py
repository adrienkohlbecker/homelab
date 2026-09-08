"""Unit tests for the scheduled pipeline full-universe status guard."""

import pytest
from conftest import load_repo_module

schedule_guard = load_repo_module("mise-tasks/ci/schedule-guard.py", name="schedule_guard")


def _jobs(
    *,
    names=("role:box", "_site_test:box"),
    statuses=("success", "success"),
    allow_failure=False,
):
    return [
        {"id": index, "name": name, "status": status, "allow_failure": allow_failure}
        for index, (name, status) in enumerate(zip(names, statuses, strict=True), start=1)
    ]


def _mock_child_api(*, child_status="success", jobs=None):
    child_jobs = _jobs() if jobs is None else jobs

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


def _pipeline(pipeline_id, status, sha="new", created_at="2026-01-02"):
    return {
        "id": pipeline_id,
        "source": "push",
        "status": status,
        "sha": sha,
        "created_at": created_at,
    }


@pytest.fixture(autouse=True)
def full_universe_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        schedule_guard,
        "_full_universe_job_names",
        lambda: {"role:box", "_site_test:box"},
    )


class TestFullUniverseChild:
    @pytest.mark.parametrize(
        ("child_status", "jobs", "expected"),
        [
            ("success", _jobs(), {"id": 20, "status": "success"}),
            ("success", _jobs(names=("role:box",), statuses=("success",)), None),
            ("failed", _jobs(statuses=("failed", "success")), {"id": 20, "status": "failed"}),
            ("success", _jobs(statuses=("manual", "manual"), allow_failure=True), None),
        ],
        ids=["green", "partial", "failed", "optional-manual"],
    )
    def test_resolves_decided_full_universe(
        self,
        monkeypatch: pytest.MonkeyPatch,
        child_status,
        jobs,
        expected,
    ) -> None:
        monkeypatch.setattr(schedule_guard, "_gl_api_get_all", _mock_child_api(child_status=child_status, jobs=jobs))

        assert schedule_guard._full_universe_child("http://api", 10, "token", "job") == expected


class TestLatestFullTestStatus:
    @pytest.mark.parametrize(
        ("parent_status", "expected_status"),
        [("manual", "success"), ("failed", "failed")],
    )
    def test_combines_parent_and_child_statuses(
        self,
        monkeypatch: pytest.MonkeyPatch,
        parent_status: str,
        expected_status: str,
    ) -> None:
        pipelines = [_pipeline(10, parent_status)]
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

        assert result == _pipeline(10, expected_status) | {
            "parent_status": parent_status,
            "child_pipeline_id": 20,
        }

    def test_partial_newer_pipeline_does_not_hide_failed_full_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pipelines = [
            _pipeline(10, "manual"),
            _pipeline(9, "failed", sha="old", created_at="2026-01-01"),
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
