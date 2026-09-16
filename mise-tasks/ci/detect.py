#!/usr/bin/env python3
# [MISE] description="Render the GitLab role-test child pipeline"
# [USAGE] flag "--target <target>" help="Qemu target to render: aws_qemu or lab"
# [USAGE] flag "--child-path <child_path>" help="Generated child pipeline path" default="test-child.yml"
# [USAGE] flag "--all" help="Force the full test universe"
"""CI change-detection pipeline (GitLab).

Resolve a green base via the GitLab pipelines API, classify the changed files,
expand dependent roles and release cells, and write a generated child pipeline:
one job per `role:variant[:ubuntu]` cell, emitted longest-first by each cell's
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
from matrix import (
    TestCell,
    build_dispatch_matrix,
    build_test_matrix,
    cell_to_ci_spec,
    cells_to_ci_specs,
    ci_spec_to_cell,
    list_testable_roles,
    load_role_test_config,
)

# Changes that cannot be attributed to individual roles test the full universe.
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

# Machine-wide fixtures fan out only to that machine. Pug remains an on-demand
# fixture, so its host vars do not select CI cells.
MACHINE_UNIVERSE_PATTERNS: list[tuple[str, str]] = [
    (r"host_vars/box\.yml", "box"),
    (r"host_vars/lab\.yml", "lab"),
    (r"host_vars/lab-qemu\.yml", "lab"),
    (r"host_vars/minimal\.yml", "minimal"),
    (r"test/minimal/.+", "minimal"),
]
_MACHINE_UNIVERSE_COMPILED = [(re.compile(r"^" + pat + r"$"), machine) for pat, machine in MACHINE_UNIVERSE_PATTERNS]


PACKER_PATH_PREFIXES = ("packer/", "mise-tasks/packer/")

FULL_UNIVERSE_RE = re.compile(r"^(" + "|".join(FULL_UNIVERSE_PATTERNS) + r")$")
ROLE_PATH_RE = re.compile(r"^roles/([^/]+)/")


class ChangeClassification(NamedTuple):
    direct_roles: list[str]
    full_universe_paths: list[str]
    packer_changed: bool
    machine_universe: set[str]


def classify_changed_files(paths: list[str]) -> ChangeClassification:
    """Classify changed file paths into CI-relevant categories."""
    roles: set[str] = set()
    full_universe: list[str] = []
    packer_changed = False
    machine_universe: set[str] = set()

    for path in paths:
        if not path:
            continue
        if FULL_UNIVERSE_RE.match(path):
            full_universe.append(path)
        if path.startswith(PACKER_PATH_PREFIXES):
            packer_changed = True
        for pat, machine in _MACHINE_UNIVERSE_COMPILED:
            if pat.match(path):
                machine_universe.add(machine)
                break
        m = ROLE_PATH_RE.match(path)
        if m:
            roles.add(m.group(1))

    return ChangeClassification(
        direct_roles=sorted(roles),
        full_universe_paths=full_universe,
        packer_changed=packer_changed,
        machine_universe=machine_universe,
    )


def propagate_release_cells(
    direct_roles: list[str],
    consumers: dict[str, list[str]],
    universe: set[str],
) -> list[TestCell]:
    """Propagate release + machine cells from changed roles onto their consumers.

    For each direct role that declares ubuntu releases in meta/test.yml,
    emit cells for every consumer that
    imports it and is in the testable universe.  The consumer's own
    machines: dict determines which machines get release cells.
    """
    extra: set[TestCell] = set()
    for role in direct_roles:
        releases = load_role_test_config(role).ubuntu
        if not releases:
            continue
        role_consumers = consumers.get(role, [])
        if not role_consumers:
            continue
        for consumer in role_consumers:
            if consumer not in universe:
                continue
            machines = load_role_test_config(consumer).machines
            for machine in machines:
                for codename in releases:
                    extra.add(TestCell(machine, codename, consumer))

    return sorted(extra)


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


def git_tree(ref: str) -> str | None:
    result = _git("rev-parse", "--verify", "--quiet", f"{ref}^{{tree}}", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


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


# Diff from the newest successful push ancestor. Shallow clones fetch candidate
# history on demand; tree matching recovers rewritten commits.

CELL_PIPELINE_SOURCES = ("push",)


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
    the retry budget on it; the caller applies the no-green fallback policy.
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
        except urllib.error.URLError, OSError:
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


def tree_equivalent_ancestor(sha: str, head: str = "HEAD", *, since: str | None = None) -> str | None:
    """Return the newest ancestor of head whose complete tree matches sha.

    A message-only history rewrite changes commit and parent IDs without changing
    the tested snapshot. Matching the complete tree recovers that green snapshot
    without accepting a genuinely divergent pipeline.

    `since` (the candidate pipeline's ``created_at``) bounds the scan: a
    rewrite's replacement is committed after the pipeline ran, so only history
    newer than that can hold the match. A rewrite that back-dates committer
    timestamps falls outside the bound and degrades toward the no-green
    full-universe path, never to a wrong base.
    """
    if git_rev_parse(sha) is None:
        git_fetch_commit(sha)
    tree = git_tree(sha)
    if tree is None:
        return None
    log_args = ["log", "--first-parent", "--format=%H %T"]
    margin = _shallow_since_arg(since) if since else None
    if margin:
        log_args.append(f"--since={margin}")
    result = _git(*log_args, head, check=False)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        commit, commit_tree = line.split()
        if commit_tree == tree:
            return commit
    return None


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
    """Newest successful push ancestor on a branch."""
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
            if not sha or source not in CELL_PIPELINE_SOURCES:
                continue
            if is_local_ancestor(sha, head_sha, since=created, branch=branch):
                log_fn(f"  green ancestor: {sha[:12]} ({created}, {source})")
                return pipe
            equivalent = tree_equivalent_ancestor(sha, head_sha, since=created)
            if equivalent:
                log_fn(f"  green tree-equivalent ancestor: {equivalent[:12]} (pipeline {sha[:12]}, {created})")
                return {**pipe, "sha": equivalent}
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
    """Resolve the newest green pipeline ancestor (branch, then default)."""
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


# Cells are emitted longest-first so the slowest jobs get the lowest build ids
# before short ones. Median successful-job durations from recent pushes keep
# failed pipelines useful and smooth cold-cache outliers. Unmeasured cells go
# first because they may be slow.
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


def _collect_cell_jobs(project_api: str, pipeline_id: int, token: str, token_kind: str) -> dict[str, dict]:
    """Jobs from the pipeline's test_cells child, keyed by name.

    Duplicate names from retried jobs collapse to the highest job id.
    """
    bridges = _gl_api_get_all(f"{project_api}/pipelines/{pipeline_id}/bridges", token, token_kind) or []
    bridge = next((item for item in bridges if item.get("name") == "test_cells"), None)
    child_id = ((bridge or {}).get("downstream_pipeline") or {}).get("id")
    if not child_id:
        return {}

    by_name: dict[str, dict] = {}
    for job in _gl_api_get_all(f"{project_api}/pipelines/{child_id}/jobs", token, token_kind) or []:
        existing = by_name.get(job["name"])
        if existing is None or job["id"] > existing["id"]:
            by_name[job["name"]] = job
    return by_name


def _recent_pipeline_ids(branch: str, project_api: str, token: str, token_kind: str, limit: int) -> list[int]:
    """Up to `limit` recent push pipeline ids, newest first, at any status."""
    params = urllib.parse.urlencode({"ref": branch, "order_by": "id", "sort": "desc", "per_page": 100})
    data = _gl_api_get(f"{project_api}/pipelines?{params}", token, token_kind=token_kind)
    if not data:
        return []
    ids = [p["id"] for p in data if p.get("id") and p.get("source") in CELL_PIPELINE_SOURCES]
    return ids[:limit]


def _cell_runtimes(branch: str, log, *, sample: int = RUNTIME_SAMPLE_PIPELINES) -> dict[str, float]:
    """Median successful duration by cell across recent push pipelines."""
    creds = _gitlab_api_creds()
    if not creds:
        return {}
    project_api, token, token_kind = creds
    ids = _recent_pipeline_ids(branch, project_api, token, token_kind, sample)
    samples: dict[str, list[float]] = defaultdict(list)
    # Retries collapse to the latest attempt before sampling.
    for pid in ids:
        jobs = _collect_cell_jobs(project_api, pid, token, token_kind)
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
        with task_file.open() as fh:
            tasks = yaml.safe_load(fh)
        _walk_tasks(tasks, role, inv)
    return {k: sorted(v) for k, v in inv.items()}


def _full_universe_specs() -> list[str]:
    """All testable role specifications."""
    return cells_to_ci_specs(build_test_matrix(list_testable_roles()))


# Branch-safe read-only role used to hydrate AWS qemu images.
CELL_ROLE_ARN = "arn:aws:iam::000390721279:role/homelab-ci-cell"

TARGETS = {
    "aws_qemu": {
        "cell_runner_tag": "aws-shell-qemu",
        # Keep the critical-path converge off the role-cell pool.
        "site_runner_tag": "aws-shell-qemu-site",
        "in_aws": True,
        "baked_toolchain": True,
        "image_oidc": True,
    },
    "lab": {
        "cell_runner_tag": "lab-shell-qemu",
        "site_runner_tag": "lab-shell-qemu",
        "in_aws": False,
        "baked_toolchain": False,
        "image_oidc": False,
    },
}

_CHILD_TEMPLATE = Path(__file__).parent / "test_child.yml.j2"


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
    cells = [ci_spec_to_cell(s)._asdict() | {"spec": s} for s in specs]
    if cells:
        mid = (len(cells) + 1) // 2
        cell_groups = [{"stage": "test1", "cells": cells[:mid]}]
        if cells[mid:]:
            cell_groups.append({"stage": "test2", "cells": cells[mid:]})
    else:
        cell_groups = []
    # Stage order gives the critical-path site job the lowest build id; needs:[]
    # keeps every stage parallel. Empty pipelines still need one stage.
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


def _split_pug_cells(specs: list[str]) -> tuple[list[str], list[str]]:
    """Keep Pug local: its QEMU image is not promoted to either CI target."""
    kept: list[str] = []
    dropped: list[str] = []
    for spec in specs:
        if ci_spec_to_cell(spec).machine == "pug":
            dropped.append(spec)
        else:
            kept.append(spec)
    return kept, dropped


def _emit_gitlab(
    specs: list[str],
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
    # Pug runs only on demand and has no CI image source.
    specs, on_demand = _split_pug_cells(specs)
    specs = sort_specs_by_runtime(specs, runtimes)

    Path(child_path).write_text(render_child_pipeline(specs, site_test, target=target))

    log(f"target={target} runner={target_config['cell_runner_tag']}")
    log(f"matrix={json.dumps(specs)}")
    if on_demand:
        log(f"dropped {len(on_demand)} on-demand cell(s): {' '.join(sorted(on_demand))}")
    if specs:
        unmeasured = [s for s in specs if s not in runtimes]
        log(f"cell order: longest-first by median recent runtime ({len(unmeasured)} unmeasured cell(s) first)")
    log(f"site_test={'true' if site_test else 'false'}")
    log(f"-> {len(specs)} cell job(s){' + site converge' if site_test else ''}")
    log(f"-> wrote {child_path}")
    return 0


def _gitlab_change_matrix(green: dict | None, log) -> tuple[list[str], bool]:
    """GitLab change-detection: resolve a diff base and compute the cell matrix.

    ``green`` is the pre-resolved newest green pipeline ancestor (or None); its
    ``sha`` is the preferred diff base. Returns ``(specs, site_test)``. A
    full-universe trigger (a cross-cutting path changed, or no usable diff base)
    returns the whole universe with site_test enabled.
    """

    def full_universe(reason):
        log(f"{reason} -> testing the FULL universe")
        return _full_universe_specs(), True

    # Explicit override, then the newest green ancestor, otherwise full universe.
    ci_base_ref = os.environ.get("CI_BASE_REF", "")
    if ci_base_ref:
        base_ref = ci_base_ref
        log(f"diff base: {base_ref} (CI_BASE_REF override)")
    elif green and green.get("sha"):
        base_ref = green["sha"]
        log(f"diff base: {green['sha'][:12]} (last green pipeline)")
    else:
        return full_universe("no green base")

    if git_rev_parse(base_ref) is None:
        log(f"  base {base_ref[:12]} outside shallow checkout; fetching the commit")
        git_fetch_commit(base_ref)
    base = git_rev_parse(base_ref)
    if base is None:
        return full_universe(f"base ref '{base_ref}' does not resolve")

    changed = git_diff_files(base)
    head_short = git_rev_parse_short("HEAD")
    log(f"comparing {base[:12]}..{head_short}: {len(changed)} file(s) changed")

    classification = classify_changed_files(changed)

    if classification.full_universe_paths:
        log("full-universe paths changed:")
        for p in classification.full_universe_paths:
            log(f"     {p}")
        return full_universe("full-universe path changed")

    universe = set(list_testable_roles())
    roles: set[str] = set()

    if classification.packer_changed:
        roles.add("packer")

    if classification.machine_universe:
        for machine in sorted(classification.machine_universe):
            match_keys = {machine}
            if machine == "box":
                match_keys.add("box_deps")
            machine_roles = [r for r in universe if match_keys & set(load_role_test_config(r).machines)]
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

    release_cells = propagate_release_cells(classification.direct_roles, deps_map, universe)
    if release_cells:
        log(f"  propagated release cells: {' '.join(cell_to_ci_spec(cell) for cell in release_cells)}")

    roles_sorted = sorted(roles)
    if roles_sorted:
        log(f"roles to test: {' '.join(roles_sorted)}")
    else:
        log("no role-relevant changes; matrix will be empty")

    return cells_to_ci_specs(build_test_matrix(roles_sorted, release_cells)), False


def _cmd_gitlab(args: list[str]) -> int:
    """Emit the GitLab dynamic child pipeline (.gitlab-ci.yml's `detect` job)."""
    from argparse import ArgumentParser

    p = ArgumentParser(prog="ci:detect")
    p.add_argument("--child-path", default="test-child.yml")
    p.add_argument("--all", action="store_true", help="Force the full universe (debug)")
    p.add_argument(
        "--target",
        default=os.environ.get("HOMELAB_CI_TARGET", "aws_qemu"),
        choices=sorted(TARGETS),
        help="Render jobs for the target qemu shell runner (default: HOMELAB_CI_TARGET or aws_qemu)",
    )
    opts = p.parse_args(args)

    def log(msg):
        print(f"[detect] {msg}", file=sys.stderr)

    # Resolve the diff base independently from runtime samples.
    branch = os.environ.get("CI_COMMIT_BRANCH", "")
    head_sha = os.environ.get("CI_COMMIT_SHA", "")
    default_branch = os.environ.get("CI_DEFAULT_BRANCH", "master")
    creds = _gitlab_api_creds()
    if creds and branch and head_sha:
        project_api, token, token_kind = creds
        green = resolve_green_base_gitlab(
            project_api=project_api,
            token=token,
            token_kind=token_kind,
            branch=branch,
            head_sha=head_sha,
            default_branch=default_branch,
            log_fn=log,
        )
    else:
        log("  no green pipeline (need CI_API_V4_URL + CI_PROJECT_ID + a token + branch)")
        green = None
    runtimes = _cell_runtimes(default_branch, log)

    if opts.all:
        log("mode: --all (full universe)")
        return _emit_gitlab(_full_universe_specs(), True, opts.child_path, runtimes, log, target=opts.target)

    event = os.environ.get("CI_PIPELINE_SOURCE", "")
    roles_input = os.environ.get("ROLES", "")

    # A manual ROLES dispatch bypasses change detection.
    if roles_input:
        log(f"mode: dispatch ROLES='{roles_input}'")
        if roles_input == "ALL":
            log("ROLES=ALL -> full universe")
            return _emit_gitlab(_full_universe_specs(), True, opts.child_path, runtimes, log, target=opts.target)
        cells = build_dispatch_matrix(roles_input)
        return _emit_gitlab(cells_to_ci_specs(cells), False, opts.child_path, runtimes, log, target=opts.target)

    log(f"mode: change detection (source={event or 'local'})")
    specs, site_test = _gitlab_change_matrix(green, log)
    return _emit_gitlab(specs, site_test, opts.child_path, runtimes, log, target=opts.target)


def main() -> int:
    try:
        return _cmd_gitlab(sys.argv[1:])
    except SystemExit as exc:
        return int(exc.code or 0)


if __name__ == "__main__":
    sys.exit(main())
