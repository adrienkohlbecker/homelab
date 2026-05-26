#!/usr/bin/env python3
#MISE description="Validate roles/*/meta/test.yml against the harness's MACHINE_CHOICES"
"""
Catch typos and unknown machine names in role test metadata before they
become a confusing CI failure. Single pass over `roles/*/meta/test.yml`,
parses each as YAML, asserts `machine:` (if present) is in MACHINE_CHOICES.

Run from the repo root via `mise run lint:test-meta` (or bundled into
`mise run lint`). Exits non-zero with a per-file diagnostic on failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

# Import MACHINE_CHOICES from the test harness so the source of truth
# stays single. test/ isn't a package, so prepend it to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "test"))
from machine import MACHINE_CHOICES  # noqa: E402


def main() -> int:
    meta_files = sorted(Path("roles").glob("*/meta/test.yml"))
    errors: list[str] = []
    for meta in meta_files:
        try:
            data = yaml.safe_load(meta.read_text()) or {}
        except yaml.YAMLError as e:
            errors.append(f"{meta}: parse error: {e}")
            continue
        if not isinstance(data, dict):
            errors.append(f"{meta}: top-level must be a mapping, got {type(data).__name__}")
            continue
        machine = data.get("machine")
        if machine is not None and machine not in MACHINE_CHOICES:
            errors.append(f"{meta}: machine={machine!r} not in {sorted(MACHINE_CHOICES)}")

    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        return 1
    print(f"Validated {len(meta_files)} test.yml file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
