#!/usr/bin/env python3
"""CI change-detection pipeline.

Complete change-detection logic: mode dispatch, green-base resolution via the
GitHub API, changed-file classification, role-deps expansion, release-cell
propagation, matrix bucket splitting, and output emission.

Called from detect-roles.sh (a thin exec wrapper) via ``python3 detect.py run``.
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
from pathlib import Path
from typing import NamedTuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "test"))
from matrix import (  # noqa: E402
    _build_dispatch_matrix,
    build_test_matrix,
    cells_to_ci_specs,
    ci_spec_to_cell,
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
    r"host_vars/(box|minimal)\.yml",
    r"test/[^/]+\.py",
    r"test/inventory\.ini",
    r"test/(playbooks|minimal)/.+",
    r"ansible\.cfg",
    r"vault-client\.sh",
    r"mise\.toml",
    r"pyproject\.toml",
    r"uv\.lock",
    r"data/network_topology\.(yml|schema\.json)",
]

PACKER_PATH_PATTERNS: list[str] = [
    r"packer/",
    r"mise-tasks/packer/",
]

# Maps changed-file patterns to the packer sources they affect.
# "qemu" = all QEMU-based sources (box/lab/pug/hetzner — they share
# qemu.pkr.hcl, chroot.sh, provision.sh). "hetzner" = the upload step
# only. Any packer/ or mise-tasks/packer/ file not explicitly listed
# falls through to "qemu" (safe default).
PACKER_SOURCE_MAP: list[tuple[str, list[str]]] = [
    (r"packer/hcloud_worker\.pkr\.hcl", ["worker"]),
    (r"packer/scripts/provision_worker\.sh", ["worker"]),
    (r"mise-tasks/packer/worker\.sh", ["worker"]),
    (r"roles/github_runner/vars/main\.yml", ["worker"]),
    (r"packer/ubuntu_images\.json", ["qemu", "worker"]),
    (r"mise-tasks/packer/hetzner\.sh", ["hetzner"]),
    (r"mise-tasks/packer/_hcloud_token\.sh", ["hetzner", "worker"]),
    (r"mise-tasks/packer/hcloud-prune-snapshots\.sh", ["hetzner", "worker"]),
]
_PACKER_SOURCE_MAP_COMPILED = [(re.compile(r"^" + pat + r"$"), srcs) for pat, srcs in PACKER_SOURCE_MAP]

CI_IMAGE_INPUT_PATTERNS: list[str] = [
    r"Dockerfile",
    r"mise\.toml",
    r"pyproject\.toml",
    r"uv\.lock",
    r"packer/qemu\.pkr\.hcl",
    r"packer/hcloud_worker\.pkr\.hcl",
]

FULL_UNIVERSE_RE = re.compile(r"^(" + "|".join(FULL_UNIVERSE_PATTERNS) + r")$")
PACKER_PATHS_RE = re.compile(r"^(" + "|".join(PACKER_PATH_PATTERNS) + r")")
CI_IMAGE_INPUTS_RE = re.compile(r"^(" + "|".join(CI_IMAGE_INPUT_PATTERNS) + r")$")
ROLE_PATH_RE = re.compile(r"^roles/([^/]+)/")


# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------


class ChangeClassification(NamedTuple):
    direct_roles: list[str]
    full_universe_paths: list[str]
    packer_sources_affected: set[str]
    ci_image_changed: bool


def _packer_sources_for(path: str) -> set[str]:
    """Map a changed file to the packer sources it affects."""
    for pat, srcs in _PACKER_SOURCE_MAP_COMPILED:
        if pat.match(path):
            return set(srcs)
    if PACKER_PATHS_RE.match(path):
        return {"qemu"}
    return set()


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

    for path in paths:
        if not path:
            continue
        if FULL_UNIVERSE_RE.match(path):
            full_universe.append(path)
        packer_sources |= _packer_sources_for(path)
        if is_master_push and CI_IMAGE_INPUTS_RE.match(path):
            ci_image_changed = True
        m = ROLE_PATH_RE.match(path)
        if m:
            roles.add(m.group(1))

    return ChangeClassification(
        direct_roles=sorted(roles),
        full_universe_paths=full_universe,
        packer_sources_affected=packer_sources,
        ci_image_changed=ci_image_changed,
    )


# ---------------------------------------------------------------------------
# Matrix bucket splitting
# ---------------------------------------------------------------------------


class MatrixBuckets(NamedTuple):
    jammy: list[str]
    noble: list[str]
    resolute: list[str]
    minimal: list[str]
    lab: list[str]
    pug: list[str]


def split_matrix_buckets(specs: list[str]) -> MatrixBuckets:
    """Split CI spec strings into per-packer-dependency buckets.

    The machine field (second colon-segment) determines the bucket:
      box/box_deps + no release or jammy  ->  jammy
      box/box_deps + noble                ->  noble
      box/box_deps + resolute             ->  resolute
      lab                                 ->  lab   (needs packer_lab)
      pug                                 ->  pug   (needs packer_pug)
      anything else (minimal)             ->  minimal
    """
    jammy: list[str] = []
    noble: list[str] = []
    resolute: list[str] = []
    minimal: list[str] = []
    lab: list[str] = []
    pug: list[str] = []

    for spec in specs:
        parts = spec.split(":")
        machine = parts[1] if len(parts) >= 2 else ""
        release = parts[2] if len(parts) >= 3 else ""

        if machine == "lab":
            lab.append(spec)
        elif machine == "pug":
            pug.append(spec)
        elif machine not in ("box", "box_deps"):
            minimal.append(spec)
        elif release == "noble":
            noble.append(spec)
        elif release == "resolute":
            resolute.append(spec)
        else:
            jammy.append(spec)

    return MatrixBuckets(
        jammy=sorted(jammy),
        noble=sorted(noble),
        resolute=sorted(resolute),
        minimal=sorted(minimal),
        lab=sorted(lab),
        pug=sorted(pug),
    )


# ---------------------------------------------------------------------------
# Packer source/ubuntu matrix
# ---------------------------------------------------------------------------

ALL_PACKER_SOURCES = ["box", "pug", "lab", "hetzner"]
ALL_PACKER_UBUNTU_RELEASES = ["jammy", "noble", "resolute"]
DEFAULT_PACKER_UBUNTU_HETZNER = ["jammy"]


class PackerSources(NamedTuple):
    all: list[str]
    box: list[str]
    lab: list[str]
    pug: list[str]
    hetzner: list[str]


def compute_packer_sources(
    inputs_sources: str = "",
    *,
    affected: set[str] | None = None,
) -> PackerSources:
    """Compute packer source matrix.

    Priority: dispatch input > file-based affected set > full set.
    ``affected`` maps source-map tags (qemu, hetzner) to concrete
    packer sources: "qemu" expands to all QEMU-based sources.
    """
    if inputs_sources:
        sources = [s for s in inputs_sources.split() if s]
    elif affected is not None:
        concrete: set[str] = set()
        if "qemu" in affected:
            concrete |= set(ALL_PACKER_SOURCES)
        if "hetzner" in affected:
            concrete.add("hetzner")
        sources = sorted(concrete & set(ALL_PACKER_SOURCES))
    else:
        sources = list(ALL_PACKER_SOURCES)
    return PackerSources(
        all=sources,
        box=[s for s in sources if s == "box"],
        lab=[s for s in sources if s == "lab"],
        pug=[s for s in sources if s == "pug"],
        hetzner=[s for s in sources if s == "hetzner"],
    )


class PackerUbuntu(NamedTuple):
    box: list[str]
    lab: list[str]
    pug: list[str]
    hetzner: list[str]


def compute_packer_ubuntu(inputs_ubuntu: str = "") -> PackerUbuntu:
    """Compute packer Ubuntu release matrix from dispatch input.

    Pinned release applies to all sources.  Empty returns defaults:
    box/lab/pug build all releases, hetzner stays jammy-only.
    """
    if inputs_ubuntu:
        return PackerUbuntu(
            box=[inputs_ubuntu],
            lab=[inputs_ubuntu],
            pug=[inputs_ubuntu],
            hetzner=[inputs_ubuntu],
        )
    return PackerUbuntu(
        box=list(ALL_PACKER_UBUNTU_RELEASES),
        lab=list(ALL_PACKER_UBUNTU_RELEASES),
        pug=list(ALL_PACKER_UBUNTU_RELEASES),
        hetzner=list(DEFAULT_PACKER_UBUNTU_HETZNER),
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
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check)


def git_diff_files(base: str, head: str = "HEAD") -> list[str]:
    result = _git("diff", "--name-only", base, head)
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


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------

CI_WORKFLOW = ".github/workflows/ci.yml"
NIGHTLY_WORKFLOW = ".github/workflows/test-nightly.yml"


def _gh_api_get(
    url: str,
    token: str,
    *,
    retries: int = 4,
    retry_delay: float = 2.0,
) -> dict | None:
    """GET a GitHub REST API endpoint with retries.  Returns parsed JSON or None."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            if attempt < retries:
                time.sleep(retry_delay)
            else:
                return None
    return None


def is_ancestor_of_head(
    sha: str,
    head_sha: str,
    *,
    repo: str,
    api_url: str,
    token: str,
) -> bool:
    """True when sha is an ancestor of head_sha (via the compare API)."""
    url = f"{api_url}/repos/{repo}/compare/{sha}...{head_sha}"
    data = _gh_api_get(url, token, retries=0)
    if data is None:
        return False
    return data.get("status", "") in ("ahead", "identical")


def nightly_actually_tested(
    run_id: int,
    *,
    repo: str,
    api_url: str,
    token: str,
) -> bool:
    """True when a nightly run actually ran tests (not just the gate job)."""
    url = f"{api_url}/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"
    data = _gh_api_get(url, token, retries=0)
    if data is None:
        return False
    return any(j.get("name") != "gate" and j.get("conclusion") == "success" for j in data.get("jobs", []))


def newest_green_ancestor(
    branch: str,
    *,
    head_sha: str,
    repo: str,
    api_url: str,
    token: str,
    log_fn,
) -> str | None:
    """Find the newest green CI/nightly run on branch that is an ancestor of head_sha."""
    log_fn(f"  searching green ci/nightly runs on '{branch}'...")
    page = 1
    while True:
        params = urllib.parse.urlencode(
            {
                "branch": branch,
                "status": "success",
                "per_page": 100,
                "page": page,
            }
        )
        url = f"{api_url}/repos/{repo}/actions/runs?{params}"
        data = _gh_api_get(url, token)
        if data is None:
            log_fn(f"  runs query failed on '{branch}' (page {page})")
            return None
        runs = data.get("workflow_runs", [])
        if not runs:
            break
        for run in runs:
            path = run.get("path", "")
            event = run.get("event", "")
            sha = run.get("head_sha", "")
            created = run.get("created_at", "")
            rid = run.get("id", 0)
            if not sha:
                continue
            if not ((path == CI_WORKFLOW and event == "push") or path == NIGHTLY_WORKFLOW):
                continue
            if path == NIGHTLY_WORKFLOW and not nightly_actually_tested(rid, repo=repo, api_url=api_url, token=token):
                log_fn(f"    skip {sha[:12]} ({created}): nightly skipped its matrix (gate-only)")
                continue
            if is_ancestor_of_head(sha, head_sha, repo=repo, api_url=api_url, token=token):
                log_fn(f"  green ancestor: {sha[:12]} ({created}, {Path(path).stem})")
                return sha
            log_fn(f"    skip {sha[:12]} ({created}): not an ancestor of HEAD")
        page += 1
    log_fn(f"  no green ancestor on '{branch}'")
    return None


def resolve_green_base(
    *,
    token: str,
    repo: str,
    ref_name: str,
    head_sha: str,
    api_url: str = "https://api.github.com",
    default_branch: str = "master",
    log_fn,
) -> str | None:
    """Resolve the diff base to the newest green ancestor run."""
    if not token:
        log_fn("  no GITHUB_TOKEN -- cannot query run history")
        return None
    if not repo or not ref_name or not head_sha:
        return None
    sha = newest_green_ancestor(
        ref_name,
        head_sha=head_sha,
        repo=repo,
        api_url=api_url,
        token=token,
        log_fn=log_fn,
    )
    if sha is None and ref_name != default_branch:
        log_fn(f"  none on '{ref_name}'; falling back to default branch '{default_branch}'")
        sha = newest_green_ancestor(
            default_branch,
            head_sha=head_sha,
            repo=repo,
            api_url=api_url,
            token=token,
            log_fn=log_fn,
        )
    return sha


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


def _cmd_classify(args: list[str]) -> int:
    is_master = "--master-push" in args
    paths = [line.strip() for line in sys.stdin if line.strip()]
    result = classify_changed_files(paths, is_master_push=is_master)
    d = result._asdict()
    d["packer_sources_affected"] = sorted(d["packer_sources_affected"])
    print(json.dumps(d))
    return 0


def _cmd_split_buckets(args: list[str]) -> int:
    if not args:
        print("usage: detect.py split-buckets <json-array>", file=sys.stderr)
        return 2
    specs = json.loads(args[0])
    result = split_matrix_buckets(specs)
    print(json.dumps(result._asdict()))
    return 0


def _cmd_packer_sources(args: list[str]) -> int:
    inputs = args[0] if args else ""
    result = compute_packer_sources(inputs)
    print(json.dumps(result._asdict()))
    return 0


def _cmd_packer_ubuntu(args: list[str]) -> int:
    inputs = args[0] if args else ""
    result = compute_packer_ubuntu(inputs)
    print(json.dumps(result._asdict()))
    return 0


def _cmd_emit(args: list[str]) -> int:
    """Emit all CI outputs: matrix buckets, packer sources/ubuntu, flags.

    Writes key=value lines to $GITHUB_OUTPUT (CI) or stdout (local).
    """
    from argparse import ArgumentParser

    p = ArgumentParser()
    p.add_argument("--matrix", required=True)
    p.add_argument("--packer-changed", default="false")
    p.add_argument("--packer-worker-changed", default="false")
    p.add_argument("--ci-image-changed", default="false")
    p.add_argument("--inputs-sources", default="")
    p.add_argument("--inputs-ubuntu", default="")
    p.add_argument("--packer-sources-affected", default="")
    opts = p.parse_args(args)

    specs = json.loads(opts.matrix)
    matrix_str = json.dumps(specs)
    buckets = split_matrix_buckets(specs)
    affected = set(opts.packer_sources_affected.split(",")) if opts.packer_sources_affected else None
    packer = compute_packer_sources(opts.inputs_sources, affected=affected)
    ubuntu = compute_packer_ubuntu(opts.inputs_ubuntu)

    pairs = [
        ("matrix", matrix_str),
        ("matrix_jammy", json.dumps(buckets.jammy)),
        ("matrix_noble", json.dumps(buckets.noble)),
        ("matrix_resolute", json.dumps(buckets.resolute)),
        ("matrix_minimal", json.dumps(buckets.minimal)),
        ("matrix_lab", json.dumps(buckets.lab)),
        ("matrix_pug", json.dumps(buckets.pug)),
        ("packer_changed", opts.packer_changed),
        ("packer_worker_changed", opts.packer_worker_changed),
        ("ci_image_changed", opts.ci_image_changed),
        ("packer_sources", json.dumps(packer.all)),
        ("packer_sources_box", json.dumps(packer.box)),
        ("packer_sources_lab", json.dumps(packer.lab)),
        ("packer_sources_pug", json.dumps(packer.pug)),
        ("packer_sources_hetzner", json.dumps(packer.hetzner)),
        ("packer_ubuntu_box", json.dumps(ubuntu.box)),
        ("packer_ubuntu_lab", json.dumps(ubuntu.lab)),
        ("packer_ubuntu_pug", json.dumps(ubuntu.pug)),
        ("packer_ubuntu_hetzner", json.dumps(ubuntu.hetzner)),
    ]

    log_parts = [
        f"matrix={matrix_str}",
        f"(jammy={json.dumps(buckets.jammy)}"
        f" noble={json.dumps(buckets.noble)}"
        f" resolute={json.dumps(buckets.resolute)}"
        f" minimal={json.dumps(buckets.minimal)}"
        f" lab={json.dumps(buckets.lab)}"
        f" pug={json.dumps(buckets.pug)})",
        f"packer_changed={opts.packer_changed}",
        f"packer_worker_changed={opts.packer_worker_changed}",
        f"ci_image_changed={opts.ci_image_changed}",
        f"packer_sources={json.dumps(packer.all)}"
        f" (box={json.dumps(packer.box)}"
        f" lab={json.dumps(packer.lab)}"
        f" pug={json.dumps(packer.pug)}"
        f" hetzner={json.dumps(packer.hetzner)})",
        f"packer_ubuntu=(box={json.dumps(ubuntu.box)}"
        f" lab={json.dumps(ubuntu.lab)}"
        f" pug={json.dumps(ubuntu.pug)}"
        f" hetzner={json.dumps(ubuntu.hetzner)})",
    ]
    print(f"[detect-roles] result: {' '.join(log_parts)}", file=sys.stderr)

    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a") as f:
            for k, v in pairs:
                f.write(f"{k}={v}\n")
    else:
        for k, v in pairs:
            print(f"{k}={v}")

    return 0


def _emit_result(
    matrix: str,
    *,
    packer_affected: set[str] | None = None,
    ci_image_changed: bool = False,
) -> int:
    """Emit CI outputs via _cmd_emit with current env for sources/ubuntu."""
    affected = packer_affected or set()
    packer_changed = "true" if affected & {"qemu", "hetzner"} else "false"
    packer_worker_changed = "true" if "worker" in affected else "false"
    affected_str = ",".join(sorted(affected)) if affected else ""
    return _cmd_emit(
        [
            "--matrix",
            matrix,
            "--packer-changed",
            packer_changed,
            "--packer-worker-changed",
            packer_worker_changed,
            "--ci-image-changed",
            "true" if ci_image_changed else "false",
            "--packer-sources-affected",
            affected_str,
            "--inputs-sources",
            os.environ.get("INPUTS_SOURCES", ""),
            "--inputs-ubuntu",
            os.environ.get("INPUTS_UBUNTU", ""),
        ]
    )


def _full_universe_matrix() -> str:
    """JSON array of all testable role specs."""
    return json.dumps(cells_to_ci_specs(build_test_matrix(list_testable_roles())))


def _cmd_run(args: list[str]) -> int:
    """Full change-detection pipeline — replaces detect-roles.sh."""

    def log(msg):
        print(f"[detect-roles] {msg}", file=sys.stderr)

    if "--all" in args:
        log("mode: --all (full universe)")
        return _emit_result(_full_universe_matrix())

    event = os.environ.get("GITHUB_EVENT_NAME", "")
    inputs_roles = os.environ.get("INPUTS_ROLES", "")
    inputs_sources = os.environ.get("INPUTS_SOURCES", "")

    if event == "workflow_dispatch" and inputs_roles:
        log(f"mode: workflow_dispatch roles='{inputs_roles}'")
        if inputs_roles == "ALL":
            log("roles=ALL -> full universe")
            return _emit_result(_full_universe_matrix())
        cells = _build_dispatch_matrix(inputs_roles)
        return _emit_result(json.dumps(cells_to_ci_specs(cells)))

    if event == "workflow_dispatch" and inputs_sources:
        log(f"mode: workflow_dispatch sources='{inputs_sources}' (packer-only, empty test matrix)")
        return _emit_result("[]", packer_affected={"qemu", "hetzner"})

    head_sha = os.environ.get("GITHUB_SHA", "")
    ref_name = os.environ.get("GITHUB_REF_NAME", "")
    github_ref = os.environ.get("GITHUB_REF", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    default_branch = os.environ.get("CI_DEFAULT_BRANCH", "master")

    log(
        f"mode: change detection (event={event or 'local'}, "
        f"branch={ref_name or '?'}, sha={head_sha[:12] if head_sha else '?'})"
    )

    def full_universe(reason, packer_affected=None, ci_image_changed=False):
        log(f"{reason} -> testing the FULL universe")
        return _emit_result(_full_universe_matrix(), packer_affected=packer_affected, ci_image_changed=ci_image_changed)

    ci_base_ref = os.environ.get("CI_BASE_REF", "")
    if ci_base_ref:
        base_ref = ci_base_ref
        log(f"diff base: {base_ref} (CI_BASE_REF override)")
    elif event == "push":
        green = resolve_green_base(
            token=token,
            repo=repo,
            ref_name=ref_name,
            head_sha=head_sha,
            api_url=api_url,
            default_branch=default_branch,
            log_fn=log,
        )
        if not green:
            return full_universe("no green ancestor run found", packer_affected={"qemu", "hetzner", "worker"})
        if git_rev_parse(green) is None:
            log(f"  base {green[:12]} outside shallow checkout; fetching the commit")
            git_fetch_commit(green)
        if git_rev_parse(green) is None:
            return full_universe(f"green run {green[:12]} unreachable", packer_affected={"qemu", "hetzner", "worker"})
        base_ref = green
        log(f"diff base: {green[:12]} (last green ci run)")
    else:
        base_ref = "HEAD~1"
        log("diff base: HEAD~1 (non-push: local/preview)")

    base = git_rev_parse(base_ref)
    if base is None:
        return full_universe(f"base ref '{base_ref}' does not resolve", packer_affected={"qemu", "hetzner", "worker"})

    changed = git_diff_files(base)
    head_short = git_rev_parse_short("HEAD")
    log(f"comparing {base[:12]}..{head_short}: {len(changed)} file(s) changed")

    is_master_push = event == "push" and github_ref == "refs/heads/master"
    classification = classify_changed_files(changed, is_master_push=is_master_push)

    packer_affected = classification.packer_sources_affected or set()

    if packer_affected:
        log(f"packer sources affected: {' '.join(sorted(packer_affected))}")
    if classification.ci_image_changed:
        log("ci-image inputs changed (master push) -> ci_image_changed=true")

    if classification.full_universe_paths:
        log("full-universe paths changed:")
        for p in classification.full_universe_paths:
            log(f"     {p}")
        return full_universe(
            "full-universe path changed",
            packer_affected=packer_affected,
            ci_image_changed=classification.ci_image_changed,
        )

    universe = set(list_testable_roles())
    roles: set[str] = set()
    release_cells: list[str] = []

    if packer_affected & {"qemu", "hetzner"}:
        roles.add("packer")

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
        releases = release_ubuntu_for(role)
        if releases and consumers:
            log(f"  propagating release cells [{' '.join(releases)}] from '{role}' to its consumers")
            for consumer in consumers:
                if consumer not in universe:
                    continue
                machines = list(machines_for(consumer))
                for machine in machines:
                    for codename in releases:
                        release_cells.append(f"{consumer}:{machine}:{codename}")

    roles_sorted = sorted(roles)
    if roles_sorted:
        log(f"roles to test: {' '.join(roles_sorted)}")
    else:
        log("no role-relevant changes; matrix will be empty")

    extra = [ci_spec_to_cell(s) for s in release_cells] if release_cells else None
    matrix = json.dumps(cells_to_ci_specs(build_test_matrix(roles_sorted, extra)))

    return _emit_result(
        matrix,
        packer_affected=packer_affected,
        ci_image_changed=classification.ci_image_changed,
    )


_COMMANDS = {
    "classify": _cmd_classify,
    "split-buckets": _cmd_split_buckets,
    "packer-sources": _cmd_packer_sources,
    "packer-ubuntu": _cmd_packer_ubuntu,
    "emit": _cmd_emit,
    "run": _cmd_run,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in _COMMANDS:
        cmds = "|".join(_COMMANDS)
        print(f"usage: detect.py <{cmds}> [args...]", file=sys.stderr)
        return 2
    return _COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
