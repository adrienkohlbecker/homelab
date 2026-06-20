#!/usr/bin/env python3
"""CI change-detection pipeline (GitLab).

The ``gitlab`` subcommand (.gitlab-ci.yml's `detect` job) is the whole story:
resolve a green base via the GitLab pipelines API, classify the changed files,
expand role-deps and release cells, and write a generated child pipeline — one
job per `role:variant[:ubuntu]` cell, emitted longest-first by each cell's
median recent runtime so the slowest jobs start first.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import NamedTuple

import jinja2
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "test"))
from matrix import (  # noqa: E402
    _build_dispatch_matrix,
    build_test_matrix,
    cells_to_ci_specs,
    ci_spec_to_cell,
    drop_on_demand_cells,
    list_testable_roles,
    machines_for,
    release_ubuntu_for,
)

# ---------------------------------------------------------------------------
# Path classification regexes
# ---------------------------------------------------------------------------

# Full-universe triggers: a change to any of these can't be attributed to
# specific roles, so the full universe is tested.
FULL_UNIVERSE_PATTERNS: list[str] = [
    r"group_vars/all/[^/]+\.(yml|yaml)",
    r"group_vars/test\.yml",
    r"test/[^/]+\.py",
    r"test/inventory\.ini",
    r"test/playbooks/.+",
    r"ansible\.cfg",
    r"vault-client\.sh",
    r"mise\.toml",
    r"pyproject\.toml",
    r"uv\.lock",
    r"data/network_topology\.(yml|schema\.json)",
    r"\.gitlab-ci\.yml",
    r"mise-tasks/ci/.+",
]

# Machine-universe triggers: a change to a test-VM host_vars or fixture
# file can't be attributed to specific roles but only affects the test
# cells that run on that machine. Maps pattern -> machine name.
#
# Only box and minimal appear: a change to host_vars/lab.yml or host_vars/pug.yml
# is intentionally not a machine-universe trigger -- those are the heavyweight
# prod-faithful fixtures, so a single host_vars edit there must not fan out to
# every role that tests on them. Those roles still get a cell when their own code
# changes.
MACHINE_UNIVERSE_PATTERNS: list[tuple[str, str]] = [
    (r"host_vars/box\.yml", "box"),
    (r"host_vars/minimal\.yml", "minimal"),
    (r"test/minimal/.+", "minimal"),
]
_MACHINE_UNIVERSE_COMPILED = [(re.compile(r"^" + pat + r"$"), machine) for pat, machine in MACHINE_UNIVERSE_PATTERNS]


PACKER_PATH_PATTERNS: list[str] = [
    r"packer/",
    r"mise-tasks/packer/",
]

# Maps changed-file patterns to the packer sources they affect.
# "qemu" = all QEMU-based sources (box/lab/pug/hetzner — they share
# qemu.pkr.hcl, chroot.sh, provision.sh). "hetzner_upload" = hetzner's
# upload/prune step only (not the shared build). Any packer/ or
# mise-tasks/packer/ file not explicitly listed falls through to "qemu"
# (safe default).
PACKER_SOURCE_MAP: list[tuple[str, list[str]]] = [
    (r"packer/ubuntu_images\.json", ["qemu"]),
    (r"mise-tasks/packer/hetzner\.sh", ["hetzner_upload"]),
    (r"mise-tasks/packer/_hetzner_rescue\.sh", ["hetzner_upload"]),
    (r"mise-tasks/packer/hcloud-prune-snapshots\.sh", ["hetzner_upload"]),
]
_PACKER_SOURCE_MAP_COMPILED = [(re.compile(r"^" + pat + r"$"), srcs) for pat, srcs in PACKER_SOURCE_MAP]

CI_IMAGE_INPUT_PATTERNS: list[str] = [
    r"Dockerfile",
    r"mise\.toml",
    r"pyproject\.toml",
    r"uv\.lock",
    r"packer/qemu\.pkr\.hcl",
]

# The custom ZFSBootMenu recovery image is built out-of-band — not a role, not
# a packer source, and built by the manual zbm_build GitLab CI job rather than
# anything this matrix dispatches. zbm_changed flags changes under these paths
# for a future auto-validation hookup; nothing consumes it yet.
ZBM_PATH_PATTERNS: list[str] = [
    r"zbm/",
    r"mise-tasks/zbm/",
    r"mise\.toml",
]

FULL_UNIVERSE_RE = re.compile(r"^(" + "|".join(FULL_UNIVERSE_PATTERNS) + r")$")
PACKER_PATHS_RE = re.compile(r"^(" + "|".join(PACKER_PATH_PATTERNS) + r")")
CI_IMAGE_INPUTS_RE = re.compile(r"^(" + "|".join(CI_IMAGE_INPUT_PATTERNS) + r")$")
ZBM_PATHS_RE = re.compile(r"^(" + "|".join(ZBM_PATH_PATTERNS) + r")")
ROLE_PATH_RE = re.compile(r"^roles/([^/]+)/")


# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------


class ChangeClassification(NamedTuple):
    direct_roles: list[str]
    full_universe_paths: list[str]
    packer_sources_affected: set[str]
    ci_image_changed: bool
    machine_universe: set[str]
    zbm_changed: bool


def _packer_sources_for(path: str) -> set[str]:
    """Map a changed file to the packer sources it affects."""
    for pat, srcs in _PACKER_SOURCE_MAP_COMPILED:
        if pat.match(path):
            return set(srcs)
    if PACKER_PATHS_RE.match(path):
        return {"qemu"}
    return set()


def _machine_universe_for(path: str) -> str | None:
    """Return the machine name if path is a machine-universe trigger."""
    for pat, machine in _MACHINE_UNIVERSE_COMPILED:
        if pat.match(path):
            return machine
    return None


def classify_changed_files(
    paths: list[str],
    *,
    is_master_push: bool = False,
) -> ChangeClassification:
    """Classify changed file paths into CI-relevant categories."""
    roles: set[str] = set()
    full_universe: list[str] = []
    packer_sources: set[str] = set()
    ci_image_changed = False
    machine_universe: set[str] = set()
    zbm_changed = False

    for path in paths:
        if not path:
            continue
        if FULL_UNIVERSE_RE.match(path):
            full_universe.append(path)
        packer_sources |= _packer_sources_for(path)
        if is_master_push and CI_IMAGE_INPUTS_RE.match(path):
            ci_image_changed = True
        mu = _machine_universe_for(path)
        if mu:
            machine_universe.add(mu)
        if ZBM_PATHS_RE.match(path):
            zbm_changed = True
        m = ROLE_PATH_RE.match(path)
        if m:
            roles.add(m.group(1))

    return ChangeClassification(
        direct_roles=sorted(roles),
        full_universe_paths=full_universe,
        packer_sources_affected=packer_sources,
        ci_image_changed=ci_image_changed,
        machine_universe=machine_universe,
        zbm_changed=zbm_changed,
    )


# ---------------------------------------------------------------------------
# Release-cell propagation
# ---------------------------------------------------------------------------


def propagate_release_cells(
    direct_roles: list[str],
    consumers: dict[str, list[str]],
    role_machines: dict[str, list[str]],
    role_releases: dict[str, list[str]],
    universe: set[str],
) -> list[str]:
    """Propagate release + machine cells from changed roles onto their consumers.

    For each direct role that declares ubuntu releases in meta/test.yml,
    emit ``consumer:machine:codename`` specs for every consumer that
    imports it and is in the testable universe.  The consumer's own
    machines: dict determines which machines get release cells.
    """
    extra: set[str] = set()
    for role in direct_roles:
        releases = role_releases.get(role, [])
        if not releases:
            continue
        role_consumers = consumers.get(role, [])
        if not role_consumers:
            continue
        for consumer in role_consumers:
            if consumer not in universe:
                continue
            machines = role_machines.get(consumer, ["box"])
            for machine in machines:
                for codename in releases:
                    extra.add(f"{consumer}:{machine}:{codename}")

    return sorted(extra)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check, timeout=120)


def git_diff_files(base: str, head: str = "HEAD") -> list[str]:
    result = _git("diff", "--name-only", "--no-renames", base, head)
    return [line for line in result.stdout.strip().splitlines() if line]


def git_rev_parse(ref: str) -> str | None:
    result = _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def git_rev_parse_short(ref: str) -> str:
    result = _git("rev-parse", "--short", ref, check=False)
    return result.stdout.strip() if result.returncode == 0 else ref[:12]


def git_fetch_commit(sha: str) -> bool:
    result = _git("fetch", "--no-tags", "--quiet", "origin", sha, check=False)
    return result.returncode == 0


def git_is_shallow() -> bool:
    return _git("rev-parse", "--is-shallow-repository", check=False).stdout.strip() == "true"


def git_deepen_since(branch: str, since: str) -> bool:
    """Deepen a shallow clone so all of `branch`'s history since `since` (a date
    git understands) is present locally.

    Fetching the *branch* with --shallow-since pulls the connected tail spanning
    an old base commit up to HEAD; a bare ``git fetch origin <sha>`` only lands
    the commit as an isolated shallow graft with no parent chain, so
    ``merge-base --is-ancestor`` can't connect it. Caller must ensure the repo is
    already shallow — on a complete clone --shallow-since would *truncate*
    history.
    """
    result = _git("fetch", "--no-tags", "--quiet", f"--shallow-since={since}", "origin", branch, check=False)
    return result.returncode == 0


def _shallow_since_arg(created_at: str) -> str | None:
    """A date one day before `created_at` (a pipeline ``created_at``), for
    --shallow-since, or None when it can't be parsed.

    A commit predates the pipeline created from it, so deepening to exactly
    ``created_at`` can miss the commit; the day of margin guarantees it lands.
    """
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt - timedelta(days=1)).date().isoformat()


# ---------------------------------------------------------------------------
# GitLab API — green-base resolution
# ---------------------------------------------------------------------------
#
# Query the project's pipeline history for the newest *successful* pipeline
# whose commit is an ancestor of HEAD, and diff against that. This turns a
# red-push -> fix sequence into "retest everything since the last fully-green
# commit" rather than only the fix's own diff (all that CI_COMMIT_BEFORE_SHA
# alone covers).
#
# A green base must be a pipeline that actually *ran the cell matrix* and passed
# it — `success` status alone is not enough. Two ways a `success` pipeline can
# carry zero cells:
#   - a `schedule` pipeline runs only the qemu-image reaper (every code job is
#     `when: never` for schedule in .gitlab-ci.yml), so it is green without
#     touching a single role;
#   - a docs-only `push` renders the lone `no_cells` placeholder into its child
#     (detect found nothing role-relevant since the prior base).
# Anchoring the diff at such a commit would skip retesting role code that was
# never exercised there — exactly the red-push the schedule's green status masks.
# So a candidate is accepted only when its triggered child pipeline holds at
# least one real cell job (see _pipeline_ran_cells). The `web` source (manual
# ROLES dispatch — a partial matrix) and `parent_pipeline` (the cell child
# itself, same sha as its push parent) are dropped up front by GREEN_BASE_SOURCES.
#
# Ancestry is checked locally with git (merge-base --is-ancestor), fetching the
# candidate on demand when it sits outside the shallow checkout — the same
# fetch-on-demand fallback the base resolver already uses, so it needs no
# second API surface (e.g. a server-side compare). The cheap cells-ran API check
# runs first, so a reaper-only schedule is rejected before any git deepen.

GREEN_BASE_SOURCES = ("push", "schedule")


def _gl_api_get(
    url: str,
    token: str,
    *,
    token_kind: str = "job",
    retries: int = 4,
    retry_delay: float = 2.0,
) -> dict | list | None:
    """GET a GitLab REST API endpoint with retries.  Returns parsed JSON or None.

    token_kind selects the auth header: "private" for a PAT / project access
    token (PRIVATE-TOKEN), "job" for the pipeline's CI_JOB_TOKEN (JOB-TOKEN).
    A 401/403 is terminal (the token is wrong or lacks read_api) — don't burn
    the retry budget on it; the caller falls back to the previous-tip base.
    """
    header = "PRIVATE-TOKEN" if token_kind == "private" else "JOB-TOKEN"
    req = urllib.request.Request(url, headers={header: token})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return None
            if attempt < retries:
                time.sleep(retry_delay)
            else:
                return None
        except (urllib.error.URLError, OSError):
            if attempt < retries:
                time.sleep(retry_delay)
            else:
                return None
    return None


def is_local_ancestor(sha: str, head: str = "HEAD", *, since: str | None = None, branch: str | None = None) -> bool:
    """True when sha is an ancestor of head in the local history.

    In a shallow CI checkout sha and head can sit on opposite sides of the
    shallow boundary; fetching sha alone then lands it as an isolated graft with
    no parent chain, so merge-base can't connect them and a true ancestor reads
    as non-ancestor. When `since` (the candidate pipeline's ``created_at``) and
    `branch` are given and the clone is shallow, deepen that branch back to
    `since` first — pulling the connected tail spanning sha..head in one fetch.

    A commit is its own ancestor, so an identical sha (HEAD already green)
    resolves True.
    """
    if since and branch and git_is_shallow():
        margin = _shallow_since_arg(since)
        if margin:
            git_deepen_since(branch, margin)
    if git_rev_parse(sha) is None:
        git_fetch_commit(sha)
    if git_rev_parse(sha) is None:
        return False
    return _git("merge-base", "--is-ancestor", sha, head, check=False).returncode == 0


def _pipeline_ran_cells(project_api: str, pipeline_id: int, token: str, token_kind: str) -> bool:
    """True when the pipeline's triggered child holds at least one real cell job.

    The cell matrix lives in the child pipeline behind the `test_cells` bridge;
    detect writes a lone `no_cells` placeholder when nothing is role-relevant, and
    a schedule pipeline (reaper only) triggers no child at all. Either way the
    pipeline can be `success` without a cell having executed, so it is not a valid
    green base — anchoring the diff there would skip retesting role code that was
    never run at that commit. Any child job other than `no_cells` is a cell; the
    parent's `success` status (with `strategy: depend`) already implies it passed.
    """
    for bridge in _gl_api_get_all(f"{project_api}/pipelines/{pipeline_id}/bridges", token, token_kind) or []:
        child = bridge.get("downstream_pipeline") or {}
        child_id = child.get("id")
        if not child_id:
            continue
        for job in _gl_api_get_all(f"{project_api}/pipelines/{child_id}/jobs", token, token_kind) or []:
            if job.get("name") != "no_cells":
                return True
    return False


def newest_green_pipeline(
    branch: str,
    *,
    head_sha: str,
    project_api: str,
    token: str,
    token_kind: str,
    log_fn,
    max_pages: int = 5,
) -> dict | None:
    """Find the newest successful push/schedule pipeline on branch that ran the
    cell matrix and is an ancestor of head.

    Returns the pipeline object (its ``sha`` is the diff base; its ``id`` keys
    the per-cell runtime lookup), or None when none resolves.
    """
    log_fn(f"  searching green pipelines on '{branch}'...")
    page = 1
    while page <= max_pages:
        params = urllib.parse.urlencode(
            {
                "ref": branch,
                "status": "success",
                "order_by": "id",
                "sort": "desc",
                "per_page": 100,
                "page": page,
            }
        )
        url = f"{project_api}/pipelines?{params}"
        data = _gl_api_get(url, token, token_kind=token_kind)
        if data is None:
            log_fn(f"  pipelines query failed on '{branch}' (page {page})")
            return None
        if not data:
            break
        for pipe in data:
            sha = pipe.get("sha", "")
            source = pipe.get("source", "")
            created = pipe.get("created_at", "")
            if not sha or source not in GREEN_BASE_SOURCES:
                continue
            # Cheap API check before the (possibly git-deepening) ancestry test:
            # reject reaper-only schedules and no_cells docs pushes outright.
            if not _pipeline_ran_cells(project_api, pipe["id"], token, token_kind):
                log_fn(f"    skip {sha[:12]} ({created}, {source}): no cells executed")
                continue
            if is_local_ancestor(sha, head_sha, since=created, branch=branch):
                log_fn(f"  green ancestor: {sha[:12]} ({created}, {source})")
                return pipe
            log_fn(f"    skip {sha[:12]} ({created}): not an ancestor of HEAD")
        page += 1
    log_fn(f"  no green ancestor on '{branch}' (searched {page - 1} page(s))")
    return None


def resolve_green_base_gitlab(
    *,
    project_api: str,
    token: str,
    token_kind: str,
    branch: str,
    head_sha: str,
    default_branch: str = "master",
    log_fn,
) -> dict | None:
    """Resolve the newest green pipeline ancestor (branch, then default).

    Returns the pipeline object, whose ``sha`` is the diff base and whose
    ``id`` keys the per-cell runtime lookup.
    """
    if not (project_api and token and branch and head_sha):
        return None
    pipe = newest_green_pipeline(
        branch,
        head_sha=head_sha,
        project_api=project_api,
        token=token,
        token_kind=token_kind,
        log_fn=log_fn,
    )
    if pipe is None and branch != default_branch:
        log_fn(f"  none on '{branch}'; falling back to default branch '{default_branch}'")
        pipe = newest_green_pipeline(
            default_branch,
            head_sha=head_sha,
            project_api=project_api,
            token=token,
            token_kind=token_kind,
            log_fn=log_fn,
        )
    return pipe


def _gitlab_api_creds() -> tuple[str, str, str] | None:
    """``(project_api, token, token_kind)`` from CI env, or None when unavailable.

    Prefers an explicit read_api token (GITLAB_API_TOKEN); falls back to the
    pipeline's CI_JOB_TOKEN.
    """
    api_url = os.environ.get("CI_API_V4_URL", "")
    project_id = os.environ.get("CI_PROJECT_ID", "")
    pat = os.environ.get("GITLAB_API_TOKEN", "")
    job_token = os.environ.get("CI_JOB_TOKEN", "")
    token, token_kind = (pat, "private") if pat else (job_token, "job")
    if not (api_url and project_id and token):
        return None
    return f"{api_url}/projects/{project_id}", token, token_kind


def _gitlab_green_base(branch: str, head_sha: str, default_branch: str, log) -> dict | None:
    """Gather GitLab CI env and resolve the newest green pipeline ancestor.

    Returns the pipeline object (``sha`` = diff base, ``id`` = runtime-lookup
    key), or None — the caller then drops to the previous-tip base and emits
    cells in default order — when no token is present, the API is unreachable,
    or the token is rejected.
    """
    creds = _gitlab_api_creds()
    if not (creds and head_sha and branch):
        log("  no green pipeline (need CI_API_V4_URL + CI_PROJECT_ID + a token + branch)")
        return None
    project_api, token, token_kind = creds
    return resolve_green_base_gitlab(
        project_api=project_api,
        token=token,
        token_kind=token_kind,
        branch=branch,
        head_sha=head_sha,
        default_branch=default_branch,
        log_fn=log,
    )


# ---------------------------------------------------------------------------
# GitLab API — cell runtime ordering
# ---------------------------------------------------------------------------
#
# Cells are emitted longest-first so the slowest jobs get the lowest build ids
# and a runner claims them before the short ones (GitLab seeds ids in YAML order
# within a stage). Runtimes are per-cell job `duration` (execution time, matched
# by job name == cell spec); a cell with no recorded runtime is emitted first,
# so a new/unmeasured -- possibly long -- cell is never left to start last.
#
# Each cell's runtime is the *median* of its successful job durations across the
# last RUNTIME_SAMPLE_PIPELINES pipelines on the default branch. Three things
# make this work where the obvious "read the green base's jobs" does not:
#
#   - Sample *successful cell jobs*, not whole-green pipelines. A pipeline with
#     one flaky cell is 'failed' overall, but its 100+ passing cells are
#     perfectly good duration samples; filtering to status=success pipelines
#     discards nearly every run and starves the table to a cell or two.
#   - Aggregate across many pipelines. A single push tests only the cells in its
#     own diff (usually a handful), so one pipeline leaves most cells unmeasured
#     and the order collapses to the alphabetical name tie-break.
#   - Take the median, not the latest. A single run can be a cold-cache outlier
#     or a fast partial; the median over recent runs is the stable estimate.
#
# Durations are only an ordering estimate, so pipeline status and ancestry are
# both irrelevant; any recent run that finished the cell is a fine sample.
RUNTIME_SAMPLE_PIPELINES = 10


def _gl_api_get_all(base_url: str, token: str, token_kind: str, *, max_pages: int = 10) -> list | None:
    """GET every page of a GitLab list endpoint (per_page=100).  None on failure."""
    items: list = []
    page = 1
    while page <= max_pages:
        sep = "&" if "?" in base_url else "?"
        data = _gl_api_get(f"{base_url}{sep}per_page=100&page={page}", token, token_kind=token_kind)
        if data is None:
            return None
        if not isinstance(data, list) or not data:
            break
        items.extend(data)
        if len(data) < 100:
            break
        page += 1
    return items


def _collect_pipeline_jobs(
    project_api: str, pipeline_id: int, token: str, token_kind: str, seen: set[int]
) -> dict[str, dict]:
    """All jobs of a pipeline and its downstream child pipelines, keyed by name.

    The cells live in a triggered child, so bridges are recursed. Duplicate
    names (retries, or a name present at two levels) collapse to the highest
    job id -- the latest attempt.
    """
    if pipeline_id in seen:
        return {}
    seen.add(pipeline_id)

    by_name: dict[str, dict] = {}

    def keep(job: dict) -> None:
        existing = by_name.get(job["name"])
        if existing is None or job["id"] > existing["id"]:
            by_name[job["name"]] = job

    for job in _gl_api_get_all(f"{project_api}/pipelines/{pipeline_id}/jobs", token, token_kind) or []:
        keep(job)
    for bridge in _gl_api_get_all(f"{project_api}/pipelines/{pipeline_id}/bridges", token, token_kind) or []:
        downstream = bridge.get("downstream_pipeline") or {}
        if downstream.get("id"):
            for job in _collect_pipeline_jobs(project_api, downstream["id"], token, token_kind, seen).values():
                keep(job)
    return by_name


def _recent_pipeline_ids(branch: str, project_api: str, token: str, token_kind: str, limit: int) -> list[int]:
    """Up to `limit` recent push/schedule pipeline ids on branch, newest first, any status.

    No status filter: runtime samples come from individual *successful cell
    jobs* (see _cell_runtimes), so a pipeline that failed overall on a flaky
    cell still contributes its passing cells. web/manual and parent_pipeline
    sources are dropped -- they re-run a hand-picked subset, not the matrix.
    """
    params = urllib.parse.urlencode({"ref": branch, "order_by": "id", "sort": "desc", "per_page": 100})
    data = _gl_api_get(f"{project_api}/pipelines?{params}", token, token_kind=token_kind)
    if not data:
        return []
    ids = [p["id"] for p in data if p.get("id") and p.get("source") in GREEN_BASE_SOURCES]
    return ids[:limit]


def _cell_runtimes(branch: str, log, *, sample: int = RUNTIME_SAMPLE_PIPELINES) -> dict[str, float]:
    """Map cell-job name -> median `duration` (s), sampled from recent pipelines; {} when unavailable.

    Collects the durations of *successful* cell jobs across the last `sample`
    pipelines on `branch` and takes the median per cell name. Sampling
    successful jobs (not whole-green pipelines) and aggregating across runs is
    what keeps the table dense: most pipelines carry a flaky cell or two and are
    'failed' overall, yet their passing cells are valid samples. Returns an
    empty map -- the caller then emits cells in default order -- when the
    API/token is unavailable.
    """
    creds = _gitlab_api_creds()
    if not creds:
        return {}
    project_api, token, token_kind = creds
    ids = _recent_pipeline_ids(branch, project_api, token, token_kind, sample)
    seen: set[int] = set()
    samples: dict[str, list[float]] = defaultdict(list)
    # _collect_pipeline_jobs collapses retries within a pipeline to the latest
    # attempt, so each pipeline yields at most one success sample per cell.
    for pid in ids:
        jobs = _collect_pipeline_jobs(project_api, pid, token, token_kind, seen)
        for name, job in jobs.items():
            if job.get("status") == "success" and job.get("duration") is not None:
                samples[name].append(job["duration"])
    runtimes = {name: median(durations) for name, durations in samples.items()}
    log(f"  runtime ordering: {len(runtimes)} cell duration(s) from {len(ids)} recent pipeline(s)")
    return runtimes


def sort_specs_by_runtime(specs: list[str], runtimes: dict[str, float]) -> list[str]:
    """Order cell specs longest-first by their median recent runtime.

    A spec with no recorded runtime (a new cell, or no runtime data at all)
    sorts first so an unmeasured -- potentially long -- cell starts before the
    measured ones. Ties and the no-runtime group break by spec name, so the
    order is deterministic.
    """
    return sorted(specs, key=lambda s: (s in runtimes, -runtimes.get(s, 0.0), s))


# ---------------------------------------------------------------------------
# Role dependency map
# ---------------------------------------------------------------------------


def _walk_tasks(tasks, role: str, inv: dict) -> None:
    """Recurse a task list, collecting import/include_role references."""
    if not isinstance(tasks, list):
        return
    for t in tasks:
        if not isinstance(t, dict):
            continue
        for k in ("import_role", "include_role"):
            body = t.get(k)
            if isinstance(body, dict) and "name" in body:
                inv[body["name"]].add(role)
        for nest in ("block", "rescue", "always"):
            if nest in t:
                _walk_tasks(t[nest], role, inv)


def build_role_deps_map() -> dict[str, list[str]]:
    """Build helper -> [consumers] inverse dependency map."""
    inv: dict[str, set[str]] = defaultdict(set)
    for task_file in sorted(Path("roles").glob("*/tasks/*.yml")):
        role = task_file.parts[-3]
        try:
            with task_file.open() as fh:
                tasks = yaml.safe_load(fh)
        except Exception:
            continue
        _walk_tasks(tasks, role, inv)
    return {k: sorted(v) for k, v in inv.items()}


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------


def _full_universe_matrix() -> str:
    """JSON array of all testable role specs."""
    return json.dumps(cells_to_ci_specs(build_test_matrix(list_testable_roles())))


# ---------------------------------------------------------------------------
# GitLab dynamic child pipeline
# ---------------------------------------------------------------------------
#
# The `detect` job emits a *generated child pipeline*: one qemu test cell per
# job. Both targets (`aws_qemu`, `lab`) run on a qemu shell runner — they render
# the same matrix and hydrate the promoted qemu image bundle before calling the
# qemu harness directly. aws_qemu reads the bundles from AWS S3 (assuming the
# cell role via GitLab OIDC); lab reads them from the on-LAN MinIO mirror with
# static creds. They differ only in which shell runner claims the cells, whether
# the guest egresses through AWS, whether the host ships a packer-baked
# toolchain, and how the cell authenticates to the image store.
#
# The parent `detect` job (.gitlab-ci.yml) runs `detect.py gitlab`, which writes
# the child YAML (one job per cell, all extending a shared `.cell` scaffold);
# the `test_cells` trigger always includes it, so an empty matrix is carried as
# a single no-op placeholder job rather than a runtime-gated trigger.

# IAM role the qemu cells assume (via GitLab OIDC) to read the promoted qemu
# image bundles from S3 (terraform/aws_ci.tf); its OIDC trust accepts any
# branch, so feature-branch pushes can test too.
CELL_ROLE_ARN = "arn:aws:iam::000390721279:role/homelab-ci-cell"
EMPTY_SHA = "0" * 40

# Both targets run the qemu backend; they differ only in the fields below.
#   cell_runner_tag — which shell runner claims the cells.
#   site_runner_tag — which runner claims the _site_test critical-path cell. On
#                     aws_qemu this becomes "aws-shell-qemu-site" (a dedicated
#                     single-host 4-vCPU pool) once that runner + ASG are live;
#                     until then it equals cell_runner_tag so site_test rides the
#                     role-cell pool and merging this can never strand the job on
#                     an unregistered runner. lab has one shell runner, so it
#                     always equals cell_runner_tag there.
#   in_aws          — true when the guest egresses through AWS, so roles pick
#                     the in-region EC2 mirrors + public DNS over the LAN Nexus
#                     / AdGuard VIP (surfaced as HOMELAB_TEST_IN_AWS).
#   baked_toolchain — true when the shell host is the packer-baked qemu-host AMI
#                     (qemu_host.pkr.hcl), which ships the mise tool tree + uv
#                     cache at /opt; the cell points mise/uv at them so a fresh
#                     host skips the toolchain re-download.
#   image_oidc      — true when the cell assumes the AWS bake/cell role via
#                     GitLab OIDC to hydrate the promoted qemu image bundles
#                     from AWS S3. False means the cell boots images already on
#                     local disk and skips hydration entirely: the lab bake
#                     writes them into lab's /mnt/scratch/homelab_ci and the
#                     co-located cells read them in place (no object store).
TARGETS = {
    "aws_qemu": {
        "cell_runner_tag": "aws-shell-qemu",
        # Dedicated single-host 4-vCPU pool (gitlab_runner_aws_qemu_site on fox,
        # terraform ASG homelab-ci-qemu-site) so the critical-path converge runs
        # uncontended off the role-cell pool.
        "site_runner_tag": "aws-shell-qemu-site",
        "in_aws": True,
        "baked_toolchain": True,
        "image_oidc": True,
    },
    "lab": {
        # lab's shell runner is on the operator LAN: the qemu guest reaches the
        # LAN Nexus + AdGuard VIP, so it is not "in AWS"; its mise is its own.
        "cell_runner_tag": "lab-shell-qemu",
        "site_runner_tag": "lab-shell-qemu",
        "in_aws": False,
        "baked_toolchain": False,
        # Boot images straight from lab's local /mnt/scratch/homelab_ci (the lab
        # bake wrote them there); no S3/OIDC, no hydration.
        "image_oidc": False,
    },
}
TARGET_NAMES = sorted(TARGETS)

# The child pipeline is a Jinja2 template so the static scaffold (`.cell`
# before_script, artifacts, retry) is reviewable as real YAML; only the
# per-cell job loop and the site_test/no_cells branches are templated.
_CHILD_TEMPLATE = Path(__file__).parent / "test_child.yml.j2"

# The GitLab pipeline UI only renders the first 100 jobs of a stage, so the
# full-universe matrix (140+ cells) spills off-screen in a single stage. Split
# the cells evenly across two display stages (test1 / test2); the cell jobs
# carry `needs: []` (DAG form) so test2 doesn't wait on test1 — the split is
# purely a display grouping, every cell still starts in parallel.


def _parse_cell_spec(spec: str) -> dict:
    """Split a ``role:variant[:ubuntu]`` cell spec into template fields."""
    parts = spec.split(":")
    return {
        "spec": spec,
        "role": parts[0],
        "variant": parts[1] if len(parts) >= 2 else "box",
        "ubuntu": parts[2] if len(parts) >= 3 else "jammy",
    }


def _split_cells_into_stages(cells: list[dict]) -> list[dict]:
    """Split cells evenly across two display stages (test1 / test2).

    The first half lands in test1, the remainder in test2; test2 is omitted
    when there are not enough cells to fill a second stage (0 or 1 cell).
    """
    if not cells:
        return []
    mid = (len(cells) + 1) // 2
    groups = [{"stage": "test1", "cells": cells[:mid]}]
    if cells[mid:]:
        groups.append({"stage": "test2", "cells": cells[mid:]})
    return groups


def render_child_pipeline(specs: list[str], site_test: bool, target: str = "aws_qemu") -> str:
    """Render the generated child-pipeline YAML from test_child.yml.j2.

    One job per cell spec (``role:variant[:ubuntu]``), each extending the
    shared ``.cell`` scaffold; an optional site-converge job; and a no-op
    placeholder so the artifact is always a valid pipeline even when the
    downstream trigger is gated off. Cells are split evenly across two display
    stages (test1 / test2) but run as a single DAG (``needs: []``).
    """
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_CHILD_TEMPLATE.parent)),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,
    )
    template = env.get_template(_CHILD_TEMPLATE.name)
    if target not in TARGETS:
        raise ValueError(f"unsupported CI target: {target!r}")
    target_config = TARGETS[target]
    cells = [_parse_cell_spec(s) for s in specs]
    cell_groups = _split_cells_into_stages(cells)
    # _site_test is the critical-path cell (longest, ~40m). GitLab seeds build
    # ids stage-by-stage and a runner picks the lowest id first, so give it a
    # dedicated leading `site` stage: it then gets the lowest ids and a runner
    # claims it immediately instead of queueing behind the matrix. needs:[]
    # (from .cell) keeps every cell -- site_test included -- starting in
    # parallel, so the leading stage orders job ids and groups the UI without
    # gating anything. no_cells falls back to a bare test1 so the empty
    # pipeline stays valid.
    cell_stages = [g["stage"] for g in cell_groups]
    stages = (["site"] if site_test else []) + cell_stages or ["test1"]
    return template.render(
        cells=cells,
        cell_groups=cell_groups,
        stages=stages,
        site_test=site_test,
        target=target,
        cell_runner_tag=target_config["cell_runner_tag"],
        site_runner_tag=target_config["site_runner_tag"],
        in_aws=target_config["in_aws"],
        baked_toolchain=target_config["baked_toolchain"],
        image_oidc=target_config["image_oidc"],
        cell_role_arn=CELL_ROLE_ARN,
    )


def _emit_gitlab(
    matrix: str,
    site_test: bool,
    child_path: str,
    runtimes: dict[str, float],
    log,
    *,
    target: str = "aws_qemu",
) -> int:
    """Write the generated child-pipeline YAML.

    The `test_cells` trigger always runs this child (GitLab evaluates `rules`
    at pipeline-creation time, so a runtime "any cells?" flag can't gate it);
    when there is nothing to test the child carries only the `no_cells`
    placeholder, which the template adds. Cells are emitted longest-first by
    their median recent runtime so the slowest jobs start first.
    """
    if target not in TARGETS:
        raise ValueError(f"unsupported CI target: {target!r}")
    target_config = TARGETS[target]
    specs = json.loads(matrix)
    # lab/pug fixtures only run on demand -- neither qemu CI target can hydrate
    # their images -- so they never enter a generated pipeline.
    specs, on_demand = drop_on_demand_cells(specs)
    specs = sort_specs_by_runtime(specs, runtimes)

    Path(child_path).write_text(render_child_pipeline(specs, site_test, target=target))

    log(f"target={target} runner={target_config['cell_runner_tag']}")
    log(f"matrix={json.dumps(specs)}")
    if on_demand:
        log(f"dropped {len(on_demand)} on-demand lab/pug cell(s): {' '.join(sorted(on_demand))}")
    if specs:
        unmeasured = [s for s in specs if s not in runtimes]
        log(f"cell order: longest-first by median recent runtime ({len(unmeasured)} unmeasured cell(s) first)")
    log(f"site_test={'true' if site_test else 'false'}")
    log(f"-> {len(specs)} cell job(s){' + site converge' if site_test else ''}")
    log(f"-> wrote {child_path}")
    return 0


def _gitlab_change_matrix(event: str, green: dict | None, log) -> tuple[str, bool]:
    """GitLab change-detection: resolve a diff base and compute the cell matrix.

    ``green`` is the pre-resolved newest green pipeline ancestor (or None); its
    ``sha`` is the preferred diff base. Returns ``(matrix_json, site_test)``. A
    full-universe trigger (a cross-cutting path changed, or no usable diff base)
    returns the whole universe with site_test enabled.
    """
    before_sha = os.environ.get("CI_COMMIT_BEFORE_SHA", "")
    branch = os.environ.get("CI_COMMIT_BRANCH", "")
    default_branch = os.environ.get("CI_DEFAULT_BRANCH", "master")

    def full_universe(reason):
        log(f"{reason} -> testing the FULL universe")
        return _full_universe_matrix(), True

    # Diff base, in priority order:
    #   1. CI_BASE_REF                  -- explicit override (local/preview).
    #   2. newest green pipeline ancestor -- the last fully-green commit, via the
    #      GitLab pipelines API; turns a red-push -> fix sequence into "retest
    #      everything since green" instead of only the fix's own diff.
    #   3. CI_COMMIT_BEFORE_SHA         -- the previous branch tip, when no green
    #      base resolves (no/rejected token, API down, or none found).
    #   4. full universe                -- nothing to diff against (new branch).
    ci_base_ref = os.environ.get("CI_BASE_REF", "")
    if ci_base_ref:
        base_ref = ci_base_ref
        log(f"diff base: {base_ref} (CI_BASE_REF override)")
    elif green and green.get("sha"):
        base_ref = green["sha"]
        log(f"diff base: {green['sha'][:12]} (last green pipeline)")
    elif before_sha and before_sha != EMPTY_SHA:
        base_ref = before_sha
        log(f"diff base: {before_sha[:12]} (previous branch tip)")
    else:
        return full_universe("no green base and no previous commit (new branch / first push)")

    if git_rev_parse(base_ref) is None:
        log(f"  base {base_ref[:12]} outside shallow checkout; fetching the commit")
        git_fetch_commit(base_ref)
    base = git_rev_parse(base_ref)
    if base is None:
        return full_universe(f"base ref '{base_ref}' does not resolve")

    changed = git_diff_files(base)
    head_short = git_rev_parse_short("HEAD")
    log(f"comparing {base[:12]}..{head_short}: {len(changed)} file(s) changed")

    is_master_push = event == "push" and branch == default_branch
    classification = classify_changed_files(changed, is_master_push=is_master_push)

    if classification.full_universe_paths:
        log("full-universe paths changed:")
        for p in classification.full_universe_paths:
            log(f"     {p}")
        return full_universe("full-universe path changed")

    universe = set(list_testable_roles())
    roles: set[str] = set()

    if classification.packer_sources_affected & {"qemu", "hetzner_upload"}:
        roles.add("packer")

    if classification.machine_universe:
        for machine in sorted(classification.machine_universe):
            match_keys = {machine}
            if machine == "box":
                match_keys.add("box_deps")
            machine_roles = [r for r in universe if match_keys & set(machines_for(r))]
            log(f"machine-universe changed -> all {machine} roles: {' '.join(machine_roles)}")
            roles.update(machine_roles)

    deps_map = build_role_deps_map()

    for role in classification.direct_roles:
        if role in universe:
            roles.add(role)
        consumers = deps_map.get(role, [])
        if consumers:
            log(f"role '{role}' changed -> consumers: {' '.join(consumers)}")
        for consumer in consumers:
            if consumer in universe:
                roles.add(consumer)

    role_releases = {r: release_ubuntu_for(r) for r in classification.direct_roles}
    all_consumers = {c for r in classification.direct_roles for c in deps_map.get(r, []) if c in universe}
    role_machines_map = {c: list(machines_for(c)) for c in all_consumers}
    release_cells = propagate_release_cells(
        classification.direct_roles, deps_map, role_machines_map, role_releases, universe
    )
    if release_cells:
        log(f"  propagated release cells: {' '.join(release_cells)}")

    roles_sorted = sorted(roles)
    if roles_sorted:
        log(f"roles to test: {' '.join(roles_sorted)}")
    else:
        log("no role-relevant changes; matrix will be empty")

    extra = [ci_spec_to_cell(s) for s in release_cells] if release_cells else None
    matrix = json.dumps(cells_to_ci_specs(build_test_matrix(roles_sorted, extra)))
    return matrix, False


def _cmd_gitlab(args: list[str]) -> int:
    """Emit the GitLab dynamic child pipeline (.gitlab-ci.yml's `detect` job)."""
    from argparse import ArgumentParser

    p = ArgumentParser(prog="detect.py gitlab")
    p.add_argument("--child-path", default="test-child.yml")
    p.add_argument("--all", action="store_true", help="Force the full universe (debug)")
    p.add_argument(
        "--target",
        default=os.environ.get("HOMELAB_CI_TARGET", "aws_qemu"),
        choices=TARGET_NAMES,
        help="Render jobs for the target qemu shell runner (default: HOMELAB_CI_TARGET or aws_qemu)",
    )
    opts = p.parse_args(args)
    if opts.target not in TARGETS:
        p.error(f"unsupported --target {opts.target!r}; choose one of: {', '.join(TARGET_NAMES)}")

    def log(msg):
        print(f"[detect] {msg}", file=sys.stderr)

    # The newest green pipeline ancestor serves two roles, so resolve it once:
    # its commit is the change-detection diff base, and its per-cell job
    # durations order every mode's emitted cells longest-first. Returns None
    # (and runtimes {} -> default order) when the GitLab API is unavailable.
    branch = os.environ.get("CI_COMMIT_BRANCH", "")
    head_sha = os.environ.get("CI_COMMIT_SHA", "")
    default_branch = os.environ.get("CI_DEFAULT_BRANCH", "master")
    green = _gitlab_green_base(branch, head_sha, default_branch, log)
    # Diff base needs a fully-green ancestor (above); runtime ordering only needs
    # duration samples, so it draws from the default branch's recent runs
    # independently -- a starved green base no longer collapses the order.
    runtimes = _cell_runtimes(default_branch, log)

    if opts.all:
        log("mode: --all (full universe)")
        return _emit_gitlab(_full_universe_matrix(), True, opts.child_path, runtimes, log, target=opts.target)

    event = os.environ.get("CI_PIPELINE_SOURCE", "")
    roles_input = os.environ.get("ROLES", "")

    # A web/manual pipeline with a ROLES variable is an explicit dispatch:
    # test exactly those roles (or the full universe when ROLES=ALL).
    if roles_input:
        log(f"mode: dispatch ROLES='{roles_input}'")
        if roles_input == "ALL":
            log("ROLES=ALL -> full universe")
            return _emit_gitlab(_full_universe_matrix(), True, opts.child_path, runtimes, log, target=opts.target)
        cells = _build_dispatch_matrix(roles_input)
        return _emit_gitlab(
            json.dumps(cells_to_ci_specs(cells)), False, opts.child_path, runtimes, log, target=opts.target
        )

    if event == "schedule":
        log("mode: schedule (nightly full build)")
        return _emit_gitlab(_full_universe_matrix(), True, opts.child_path, runtimes, log, target=opts.target)

    log(f"mode: change detection (source={event or 'local'})")
    matrix, site_test = _gitlab_change_matrix(event, green, log)
    return _emit_gitlab(matrix, site_test, opts.child_path, runtimes, log, target=opts.target)


_COMMANDS = {
    "gitlab": _cmd_gitlab,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in _COMMANDS:
        cmds = "|".join(_COMMANDS)
        print(f"usage: detect.py <{cmds}> [args...]", file=sys.stderr)
        return 2
    return _COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
