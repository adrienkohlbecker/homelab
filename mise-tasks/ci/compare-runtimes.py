#!/usr/bin/env -S uv run --script
# [MISE] description="Compare per-job runtimes of one variant's cells across two GitLab pipelines"
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Diff cell runtimes between two GitLab pipelines, matched by job name.

Built to settle instance-type A/Bs (e.g. box c6a.large -> m6a.large): run the
same matrix on two pipelines, then read off which cells moved and by how much.
Durations come from the jobs API `duration` field (execution time, excludes
queue/startup), so the comparison is wall-clock-in-runner, not lifecycle.

Cells live in a triggered child pipeline, so each pipeline id is walked down
its bridges as well -- pass either the parent or the child id and the cell
jobs are found either way. Retried jobs collapse to the latest attempt (highest
job id). "box jobs" means the `box` variant only (the 2nd `role:variant:ubuntu`
field); box_deps / lab / pug are different machines and are filtered out unless
--variant says otherwise.

  uv run python mise-tasks/ci/compare-runtimes.py <baseline_pipeline> <candidate_pipeline>
  mise run ci:compare-runtimes -- <baseline_pipeline> <candidate_pipeline> --variant box
"""

import argparse
import json
import subprocess
import sys
import urllib.parse

PROJECT_DEFAULT = "akohlbecker/homelab"


def glab_paginate(project_enc: str, path: str) -> list[dict]:
    """GET every page of a GitLab REST list endpoint via the authed glab CLI.

    --output ndjson is deliberate: --paginate alone concatenates one JSON array
    per page (not a single mergeable document), which won't parse.
    """
    url = f"projects/{project_enc}/{path}"
    proc = subprocess.run(
        ["glab", "api", "--paginate", "--output", "ndjson", url],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"glab api {url} failed: {proc.stderr.strip()}")
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def collect_jobs(project_enc: str, pipeline_id: int, seen: set[int]) -> dict[str, dict]:
    """All jobs of a pipeline and its downstream child pipelines, by job name.

    Recurses through bridges so a parent pipeline id resolves to its child's
    cell jobs. Duplicate names (retries, or a name present at two levels)
    collapse to the highest job id -- the latest attempt.
    """
    if pipeline_id in seen:
        return {}
    seen.add(pipeline_id)

    by_name: dict[str, dict] = {}

    def keep(job: dict) -> None:
        existing = by_name.get(job["name"])
        if existing is None or job["id"] > existing["id"]:
            by_name[job["name"]] = job

    for job in glab_paginate(project_enc, f"pipelines/{pipeline_id}/jobs?per_page=100"):
        keep(job)

    for bridge in glab_paginate(project_enc, f"pipelines/{pipeline_id}/bridges?per_page=100"):
        downstream = bridge.get("downstream_pipeline") or {}
        if downstream.get("id"):
            for job in collect_jobs(project_enc, downstream["id"], seen).values():
                keep(job)

    return by_name


def variant_of(job_name: str) -> str | None:
    """The variant field of a `role:variant[:ubuntu]` cell name, else None."""
    parts = job_name.split(":")
    return parts[1] if len(parts) >= 2 else None


def fmt(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds:.0f}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("baseline", type=int, help="baseline pipeline id (the 'before')")
    ap.add_argument("candidate", type=int, help="candidate pipeline id (the 'after')")
    ap.add_argument(
        "--project",
        default=PROJECT_DEFAULT,
        help=f"project path (default {PROJECT_DEFAULT})",
    )
    ap.add_argument(
        "--variant",
        default="box",
        help="cell variant to compare (default box; 'all' for every cell)",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="emit the comparison as JSON instead of a table",
    )
    args = ap.parse_args()

    project_enc = urllib.parse.quote(args.project, safe="")
    base = collect_jobs(project_enc, args.baseline, set())
    cand = collect_jobs(project_enc, args.candidate, set())

    def wanted(name: str) -> bool:
        return args.variant == "all" or variant_of(name) == args.variant

    names = sorted(n for n in (set(base) | set(cand)) if wanted(n))

    rows = []
    for name in names:
        b, c = base.get(name), cand.get(name)
        bd = b["duration"] if b else None
        cd = c["duration"] if c else None
        bs = b["status"] if b else None
        cs = c["status"] if c else None
        # Only a delta between two *finished* successes is meaningful: a
        # running job's `duration` is a partial elapsed-so-far (e.g. 3s into
        # mise:box), and a failed job stopped early -- both would render a
        # bogus speedup. Leave delta None otherwise; the status note explains.
        both_green = bs == "success" and cs == "success"
        delta = (cd - bd) if (both_green and bd is not None and cd is not None) else None
        pct = (delta / bd * 100) if (delta is not None and bd) else None
        rows.append(
            {
                "job": name,
                "base_s": bd,
                "cand_s": cd,
                "delta_s": delta,
                "delta_pct": pct,
                "base_status": bs,
                "cand_status": cs,
            }
        )

    # Sort biggest-baseline-first so the cells that dominate wall-clock lead.
    rows.sort(key=lambda r: (r["base_s"] is None, -(r["base_s"] or 0)))

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    use_color = sys.stdout.isatty()

    def color(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if use_color else text

    print(f"baseline = pipeline {args.baseline}   candidate = pipeline {args.candidate}   variant = {args.variant}\n")
    print(f"{'JOB':35} {'BASE':>7} {'CAND':>7} {'Δs':>7} {'Δ%':>7}  NOTE")
    print("-" * 80)

    # Totals only over jobs that both ran AND both succeeded -- an apples-to-apples set.
    tot_base = tot_cand = 0.0
    comparable = 0
    for r in rows:
        note_parts = []
        if r["base_status"] not in (None, "success"):
            note_parts.append(f"base={r['base_status']}")
        if r["cand_status"] not in (None, "success"):
            note_parts.append(f"cand={r['cand_status']}")
        if r["base_s"] is None:
            note_parts.append("new")
        if r["cand_s"] is None:
            note_parts.append("dropped")

        ds = r["delta_s"]
        dp = r["delta_pct"]
        delta_str = "-" if ds is None else f"{ds:+.0f}"
        pct_str = "-" if dp is None else f"{dp:+.0f}%"
        if ds is not None:
            delta_str = color(delta_str, "32" if ds < 0 else "31")
            pct_str = color(pct_str, "32" if ds < 0 else "31")

        print(
            f"{r['job']:35} {fmt(r['base_s']):>7} {fmt(r['cand_s']):>7} "
            f"{delta_str:>{16 if use_color else 7}} {pct_str:>{16 if use_color else 7}}  {' '.join(note_parts)}"
        )

        if (
            r["base_s"] is not None
            and r["cand_s"] is not None
            and r["base_status"] == "success"
            and r["cand_status"] == "success"
        ):
            tot_base += r["base_s"]
            tot_cand += r["cand_s"]
            comparable += 1

    print("-" * 80)
    only_base = sum(1 for r in rows if r["cand_s"] is None)
    only_cand = sum(1 for r in rows if r["base_s"] is None)
    print(f"matched+success: {comparable}    only-in-baseline: {only_base}    only-in-candidate: {only_cand}")
    if tot_base:
        tot_delta = tot_cand - tot_base
        print(
            f"summed runtime (comparable cells): {tot_base:.0f}s -> {tot_cand:.0f}s "
            f"({tot_delta:+.0f}s, {tot_delta / tot_base * 100:+.0f}%)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
