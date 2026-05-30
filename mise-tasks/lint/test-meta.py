#!/usr/bin/env python3
# [MISE] description="Validate roles/*/meta/test.yml against the harness's MACHINE_CHOICES"
"""
Catch typos and unknown machine names in role test metadata before they
become a confusing CI failure. Single pass over `roles/*/meta/test.yml`,
parses each as YAML, asserts `machine:` (if present) is in MACHINE_CHOICES
and `ubuntu:` (if present) is a list of known UBUNTU_RELEASES codenames
(excluding jammy, which is the default cell -- listing it is redundant).

Run from the repo root via `mise run lint:test-meta` (or bundled into
`mise run lint`). Exits non-zero with a per-file diagnostic on failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

# Import MACHINE_CHOICES + UBUNTU_RELEASES from the test harness so the
# source of truth stays single. test/ isn't a package, so prepend it to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "test"))
from machine import DEFAULT_UBUNTU, MACHINE_CHOICES, UBUNTU_RELEASES  # noqa: E402


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

        ubuntu = data.get("ubuntu")
        if ubuntu is not None:
            if not isinstance(ubuntu, list):
                errors.append(f"{meta}: ubuntu must be a list, got {type(ubuntu).__name__}")
            else:
                for codename in ubuntu:
                    if codename not in UBUNTU_RELEASES:
                        errors.append(f"{meta}: ubuntu={codename!r} not in {sorted(UBUNTU_RELEASES)}")
                    elif codename == DEFAULT_UBUNTU:
                        # The default cell already runs jammy; an extra
                        # jammy entry just duplicates it.
                        errors.append(
                            f"{meta}: ubuntu lists {DEFAULT_UBUNTU!r}, the default release"
                            " -- omit it (only list extra releases)"
                        )

    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        return 1
    print(f"Validated {len(meta_files)} test.yml file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
