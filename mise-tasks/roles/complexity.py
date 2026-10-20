#!/usr/bin/env python3
# [MISE] description="Rank roles by complexity (lines, tasks, when: branches, test cells)"
# [USAGE] flag "--json" help="Print the ranking as JSON instead of a table"
"""Rank Ansible roles by how much there is to simplify.

Per role it measures non-blank lines across the role's text files, tasks in
tasks/ and handlers/ (block members counted, the block wrapper not), `when:`
conditions (a list counts each entry), and test cells from test/matrix.py. The
composite score is the mean percentile rank of the four, so no unit dominates.
The role_simplify workflow reads the JSON form to pick a tier.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, NamedTuple

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "test"))
from matrix import build_role_cells, list_testable_roles  # noqa: E402

METRICS = ("lines", "tasks", "whens", "cells")


class RoleComplexity(NamedTuple):
    role: str
    lines: int
    tasks: int
    whens: int
    cells: int


def count_lines(role_dir: Path) -> int:
    total = 0
    for path in role_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary payloads (images, archives) carry no reviewable lines
        total += sum(1 for line in text.splitlines() if line.strip())
    return total


def count_tasks(node: Any) -> tuple[int, int]:
    """Return (tasks, when conditions) in a parsed task list."""
    if not isinstance(node, list):
        return 0, 0
    tasks = whens = 0
    for item in node:
        if not isinstance(item, dict):
            continue
        when = item.get("when")
        if when is not None:
            whens += len(when) if isinstance(when, list) else 1
        sections = [item[key] for key in ("block", "rescue", "always") if key in item]
        if not sections:
            tasks += 1
        for section in sections:
            sub_tasks, sub_whens = count_tasks(section)
            tasks += sub_tasks
            whens += sub_whens
    return tasks, whens


def measure(role: str) -> RoleComplexity:
    role_dir = REPO_ROOT / "roles" / role
    tasks = whens = 0
    for subdir in ("tasks", "handlers"):
        for path in sorted((role_dir / subdir).glob("*.y*ml")):
            sub_tasks, sub_whens = count_tasks(yaml.safe_load(path.read_text(encoding="utf-8")))
            tasks += sub_tasks
            whens += sub_whens
    cells = len(build_role_cells(role)) if (role_dir / "meta" / "test.yml").exists() else 0
    return RoleComplexity(role, count_lines(role_dir), tasks, whens, cells)


def percentile(values: list[int], value: int) -> float:
    """Mid-rank percentile in [0, 1]; ties share the average of their ranks."""
    if len(values) < 2:
        return 0.0
    below = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    return (below + (equal - 1) / 2) / (len(values) - 1)


def rank(rows: list[RoleComplexity]) -> list[dict[str, Any]]:
    columns = {metric: [getattr(row, metric) for row in rows] for metric in METRICS}
    ranked = [
        {
            **row._asdict(),
            "score": round(sum(percentile(columns[m], getattr(row, m)) for m in METRICS) / len(METRICS), 3),
        }
        for row in rows
    ]
    ranked.sort(key=lambda r: (-r["score"], r["role"]))
    for position, entry in enumerate(ranked, start=1):
        entry["rank"] = position
    return ranked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    # matrix.py resolves roles/ relative to the working directory.
    os.chdir(REPO_ROOT)
    ranked = rank([measure(role) for role in list_testable_roles()])
    if args.json:
        print(json.dumps(ranked, indent=1))
        return 0
    print(f"{'rank':>4}  {'role':<24}{'score':>6}{'lines':>7}{'tasks':>6}{'whens':>6}{'cells':>6}")
    for r in ranked:
        print(
            f"{r['rank']:>4}  {r['role']:<24}{r['score']:>6.3f}{r['lines']:>7}{r['tasks']:>6}{r['whens']:>6}{r['cells']:>6}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
