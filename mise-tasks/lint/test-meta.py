#!/usr/bin/env python3
# [MISE] description="Validate roles/*/meta/test.yml against the harness's MACHINE_CHOICES"
"""Validate role test metadata before CI renders the qemu matrix."""

from __future__ import annotations

import sys
from pathlib import Path

# Import metadata constants from the test harness so the source of truth stays
# single. test/ isn't a package, so prepend it to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "test"))
from machine import MACHINE_CHOICES
from matrix import RoleTestConfigError, load_role_test_config

MACHINE_NAMES = sorted(MACHINE_CHOICES)


def main() -> int:
    meta_files = sorted(Path("roles").glob("*/meta/test.yml"))
    errors: list[str] = []
    for meta in meta_files:
        try:
            load_role_test_config(meta.parent.parent.name, tuple(MACHINE_NAMES))
        except RoleTestConfigError as e:
            errors.extend(e.messages)

    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        return 1
    print(f"Validated {len(meta_files)} test.yml file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
