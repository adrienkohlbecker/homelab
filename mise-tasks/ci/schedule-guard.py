#!/usr/bin/env python3
# [MISE] description="Mirror master's last full-test status onto the scheduled pipeline"
"""Status guard for scheduled pipelines.

A scheduled pipeline runs only `qemu_image_reaper` -- every code job is
`when: never` for the schedule source in .gitlab-ci.yml. That reaper-only
pipeline is `success` regardless of master's real test state, so on its own it
flips the branch's pipeline badge green and triggers GitLab's "Pipeline fixed"
email even when the last full test run failed.

This job makes the scheduled pipeline *no greener than the branch actually is*:
it finds the most recent completed pipeline that genuinely ran the cell matrix
and exits with that pipeline's verdict. Green only when the branch's last full
test passed, so the schedule can never manufacture a spurious "fixed".

The "ran the cell matrix" test reuses detect.py's `_pipeline_ran_cells` -- the
same check that already rejects a reaper-only schedule as a green diff base --
so the two stay in lock-step. Fails closed: if the branch's real verdict can't
be resolved, the guard reds rather than claim health it can't prove.
"""

import os
import sys
import urllib.parse
from pathlib import Path

# detect.py (same dir) owns the GitLab pipelines-API helpers; the path insert
# must precede the import so a bare `mise run` resolves it without the package.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from detect import (
    GREEN_BASE_SOURCES,
    _gitlab_api_creds,
    _gl_api_get,
    _pipeline_ran_cells,
)

# Only a finished, definitive verdict mirrors. A running/created/canceled
# pipeline is skipped so the guard reflects the last *decided* full test, not a
# superseded or in-flight one.
TERMINAL_STATUSES = {"success", "failed"}


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
    the matrix (push; schedules and web/manual partials are dropped), reached a
    terminal verdict, and actually executed a gating cell. The current scheduled
    pipeline is skipped via `exclude_id`.
    """
    page = 1
    while page <= max_pages:
        params = urllib.parse.urlencode(
            {"ref": branch, "order_by": "id", "sort": "desc", "per_page": 100, "page": page}
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
            if pipe.get("status") not in TERMINAL_STATUSES:
                continue
            if not _pipeline_ran_cells(project_api, pid, token, token_kind):
                continue
            return pipe
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
    log(f"last full test on '{branch}': {pipe.get('sha', '')[:12]} ({pipe.get('created_at', '')}) -> {status}")
    if status == "success":
        return 0
    log("branch's last full test was not green; reddening the scheduled pipeline to suppress a spurious 'fixed'")
    return 1


if __name__ == "__main__":
    sys.exit(main())
