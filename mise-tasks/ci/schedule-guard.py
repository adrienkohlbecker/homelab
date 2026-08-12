#!/usr/bin/env python3
# [MISE] description="Mirror master's last full-test status onto the scheduled pipeline"
"""Status guard for scheduled pipelines.

A scheduled pipeline runs only `qemu_image_reaper` -- every code job is
`when: never` for the schedule source in .gitlab-ci.yml. That reaper-only
pipeline is `success` regardless of master's real test state, so on its own it
flips the branch's pipeline badge green and triggers GitLab's "Pipeline fixed"
email even when the last full test run failed.

This job makes the scheduled pipeline *no greener than the branch actually is*:
it finds the most recent parent whose test_cells child contained the complete
emitted CI universe and exits with that pipeline's effective verdict. Green
requires both a green child and a non-failed parent; a parent waiting only on
the deliberately blocking manual image-bake jobs still counts as decided.

Fails closed: partial, optional-manual, running, canceled, or unresolvable child
pipelines cannot make the guard green.
"""

import json
import os
import sys
import urllib.parse
from functools import cache
from pathlib import Path

# detect.py (same dir) owns the GitLab pipelines-API helpers; the path insert
# must precede the import so a bare `mise run` resolves it without the package.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from detect import (
    GREEN_BASE_SOURCES,
    _full_universe_matrix,
    _gitlab_api_creds,
    _gl_api_get,
    _gl_api_get_all,
    drop_on_demand_cells,
)

# A parent can remain manual after every automatic test passed because the
# qemu-image bake jobs are deliberately blocking manual actions. The child
# verdict below distinguishes that state from an unfinished test matrix.
DECIDED_PARENT_STATUSES = {"success", "failed", "manual"}
DECIDED_CHILD_STATUSES = {"success", "failed"}
FULL_UNIVERSE_SITE_JOBS = {"_site_test:box", "_site_check:box"}


@cache
def _full_universe_job_names() -> set[str]:
    """Job names emitted by a full-universe CI child pipeline."""
    specs = json.loads(_full_universe_matrix())
    specs, _ = drop_on_demand_cells(specs)
    return set(specs) | FULL_UNIVERSE_SITE_JOBS


def _full_universe_child(
    project_api: str,
    pipeline_id: int,
    token: str,
    token_kind: str,
) -> dict | None:
    """Decided test_cells child when it contains the full gating universe."""
    expected_names = _full_universe_job_names()
    bridges = _gl_api_get_all(f"{project_api}/pipelines/{pipeline_id}/bridges", token, token_kind) or []
    for bridge in bridges:
        if bridge.get("name") != "test_cells":
            continue
        child = bridge.get("downstream_pipeline") or {}
        child_id = child.get("id")
        child_status = child.get("status")
        if not child_id or child_status not in DECIDED_CHILD_STATUSES:
            continue

        # The jobs endpoint returns only the latest attempt of each job unless
        # include_retried=true is requested, so a name maps 1:1 to its final
        # verdict.
        jobs = _gl_api_get_all(f"{project_api}/pipelines/{child_id}/jobs", token, token_kind) or []
        latest_jobs = {job["name"]: job for job in jobs if job.get("name")}

        if not expected_names.issubset(latest_jobs):
            continue
        if any(latest_jobs[name].get("allow_failure") for name in expected_names):
            continue
        return {"id": child_id, "status": child_status}
    return None


def latest_full_test_status(
    branch: str,
    *,
    project_api: str,
    token: str,
    token_kind: str,
    exclude_id: int,
    log_fn,
    max_pages: int = 3,
) -> dict | None:
    """Newest finished cell-running pipeline on `branch`, or None when none resolves.

    Walks recent pipelines newest-first and returns the first whose source ran
    the complete emitted CI universe, whose child reached a definitive verdict,
    and whose parent is either decided or waiting only on manual actions. The
    current scheduled pipeline is skipped via `exclude_id`.
    """
    page = 1
    while page <= max_pages:
        params = urllib.parse.urlencode(
            {
                "ref": branch,
                "order_by": "id",
                "sort": "desc",
                "per_page": 100,
                "page": page,
            }
        )
        data = _gl_api_get(f"{project_api}/pipelines?{params}", token, token_kind=token_kind)
        if data is None:
            log_fn(f"pipelines query failed on '{branch}' (page {page})")
            return None
        if not data:
            break
        for pipe in data:
            pid = pipe.get("id")
            if not pid or pid == exclude_id:
                continue
            if pipe.get("source") not in GREEN_BASE_SOURCES:
                continue
            parent_status = pipe.get("status")
            if parent_status not in DECIDED_PARENT_STATUSES:
                continue
            child = _full_universe_child(project_api, pid, token, token_kind)
            if child is None:
                continue
            effective_status = (
                "success" if parent_status in {"success", "manual"} and child["status"] == "success" else "failed"
            )
            return pipe | {
                "status": effective_status,
                "parent_status": parent_status,
                "child_pipeline_id": child["id"],
            }
        page += 1
    return None


def main() -> int:
    def log(msg):
        print(f"[schedule-guard] {msg}", file=sys.stderr)

    creds = _gitlab_api_creds()
    if not creds:
        log("cannot resolve GitLab API creds (need CI_API_V4_URL + CI_PROJECT_ID + a token); failing closed")
        return 1
    project_api, token, token_kind = creds

    branch = os.environ.get("CI_COMMIT_BRANCH") or os.environ.get("CI_DEFAULT_BRANCH", "master")
    exclude_id = int(os.environ.get("CI_PIPELINE_ID") or 0)

    pipe = latest_full_test_status(
        branch,
        project_api=project_api,
        token=token,
        token_kind=token_kind,
        exclude_id=exclude_id,
        log_fn=log,
    )
    if pipe is None:
        log(f"no completed full-test pipeline found on '{branch}'; failing closed")
        return 1

    status = pipe["status"]
    log(
        f"last full-universe test on '{branch}': {pipe.get('sha', '')[:12]} "
        f"({pipe.get('created_at', '')}), child #{pipe['child_pipeline_id']} -> {status} "
        f"(parent {pipe['parent_status']})"
    )
    if status == "success":
        return 0
    log("branch's last full test was not green; reddening the scheduled pipeline to suppress a spurious 'fixed'")
    return 1


if __name__ == "__main__":
    sys.exit(main())
