#!/usr/bin/env python3
"""Test matrix generation — single source of truth for CI and local runs.

Reads roles/*/meta/test.yml and .github/ci-minimal-roles.txt to produce the
(machine, ubuntu, role) cell list that both test/testall.py and
mise-tasks/ci/detect-roles.sh consume.

CLI (for detect-roles.sh):
  python3 test/matrix.py --json --all                        # full universe
  python3 test/matrix.py --json --dispatch "foo,bar:minimal"  # dispatch input
  python3 test/matrix.py --json --extra C1 C2 -- R1 R2       # push path
  python3 test/matrix.py --json                               # empty matrix

Human-readable (for local inspection):
  python3 test/matrix.py            # full universe, tab-separated
  python3 test/matrix.py foo bar    # specific roles
"""

import json
import sys
from pathlib import Path
from typing import NamedTuple

import yaml

MINIMAL_ROLES_FILE = Path(".github/ci-minimal-roles.txt")

# Canonical copies live in test/machine.py alongside the QEMU specs; keep in
# sync (lint:test-meta validates meta/test.yml against both).
UBUNTU_RELEASES: dict[str, str] = {
    "jammy": "22.04",
    "noble": "24.04",
    "resolute": "26.04",
}
DEFAULT_UBUNTU = "jammy"


class TestCell(NamedTuple):
    """A (machine, ubuntu, role) triple to test."""

    machine: str
    ubuntu: str
    role: str


def list_testable_roles() -> list[str]:
    """Return all roles with tasks/main.yml, sorted."""
    roles_dir = Path("roles")
    if not roles_dir.exists():
        return []
    return [d.name for d in sorted(roles_dir.iterdir()) if d.is_dir() and (d / "tasks" / "main.yml").exists()]


def read_minimal_roles() -> frozenset[str]:
    """Roles that get a (minimal, jammy) cell on top of their default."""
    if not MINIMAL_ROLES_FILE.exists():
        return frozenset()
    return frozenset(
        line.strip()
        for line in MINIMAL_ROLES_FILE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


def _read_role_meta(role: str) -> dict:
    meta_path = Path(f"roles/{role}/meta/test.yml")
    if not meta_path.exists():
        return {}
    try:
        return yaml.safe_load(meta_path.read_text()) or {}
    except yaml.YAMLError as e:
        print(f"error: {meta_path}: {e}", file=sys.stderr)
        sys.exit(1)


def default_machine_for(role: str) -> str:
    """Default test machine from meta/test.yml (falls back to 'box')."""
    return _read_role_meta(role).get("machine", "box")


def release_ubuntu_for(role: str) -> list[str]:
    """Extra Ubuntu releases from meta/test.yml (empty when none)."""
    return _read_role_meta(role).get("ubuntu") or []


def build_role_cells(role: str, minimal_roles: frozenset[str]) -> list[TestCell]:
    """Expand a single role into its test cells.

    - Default cell: (meta machine or box, jammy, role)
    - Minimal cell if in ci-minimal-roles.txt: (minimal, jammy, role)
    - Release cell per ubuntu in meta/test.yml: (default machine, codename, role)
      Uses the role's default machine so a box_deps role gets box_deps:resolute
      (box_deps is seeded per-release).
    """
    machine = default_machine_for(role)
    cells = [TestCell(machine, DEFAULT_UBUNTU, role)]
    if role in minimal_roles:
        cells.append(TestCell("minimal", DEFAULT_UBUNTU, role))
    for codename in release_ubuntu_for(role):
        cells.append(TestCell(machine, codename, role))
    return cells


def build_test_matrix(
    roles: list[str],
    extra_cells: list[TestCell] | None = None,
) -> list[TestCell]:
    """Build the deduplicated, sorted test matrix for the given roles.

    extra_cells: additional cells to merge (used by CI's release-cell
    propagation from changed helper roles to their consumers).
    """
    minimal_roles = read_minimal_roles()
    cells: set[TestCell] = set()
    for role in roles:
        cells.update(build_role_cells(role, minimal_roles))
    if extra_cells:
        cells.update(extra_cells)
    return sorted(cells)


def cell_to_ci_spec(cell: TestCell) -> str:
    """Format one cell as a CI spec string."""
    if cell.ubuntu == DEFAULT_UBUNTU:
        return f"{cell.role}:{cell.machine}"
    return f"{cell.role}:{cell.machine}:{cell.ubuntu}"


def cells_to_ci_specs(cells: list[TestCell]) -> list[str]:
    """Format cells as sorted, deduplicated CI spec strings."""
    return sorted({cell_to_ci_spec(c) for c in cells})


def ci_spec_to_cell(spec: str) -> TestCell:
    """Parse a CI spec string into a TestCell."""
    parts = spec.split(":")
    if len(parts) == 2:
        return TestCell(machine=parts[1], ubuntu=DEFAULT_UBUNTU, role=parts[0])
    if len(parts) == 3:
        return TestCell(machine=parts[1], ubuntu=parts[2], role=parts[0])
    raise ValueError(f"Invalid CI spec: {spec!r}")


def _build_dispatch_matrix(dispatch_input: str) -> list[TestCell]:
    """Parse a comma-separated dispatch input into cells.

    Tokens without colons are expanded via build_role_cells (with minimal
    + release escalation). Tokens with colons are exact CI specs (no
    escalation — the user said what they wanted).
    """
    universe = set(list_testable_roles())
    minimal_roles = read_minimal_roles()
    cells: list[TestCell] = []
    for token in dispatch_input.split(","):
        token = token.strip()
        if not token:
            continue
        role = token.split(":")[0]
        if role not in universe:
            print(
                f"error: role '{role}' is not in the testable universe " f"(no roles/{role}/tasks/main.yml)",
                file=sys.stderr,
            )
            sys.exit(1)
        if ":" in token:
            cells.append(ci_spec_to_cell(token))
        else:
            cells.extend(build_role_cells(token, minimal_roles))
    return cells


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate the test matrix from meta/test.yml + ci-minimal-roles.txt",
    )
    parser.add_argument("roles", nargs="*", help="Roles to expand")
    parser.add_argument("--json", action="store_true", help="Output CI-format JSON array")
    parser.add_argument("--all", action="store_true", help="Expand all testable roles")
    parser.add_argument(
        "--dispatch",
        metavar="INPUT",
        help="Parse comma-separated dispatch input (role or role:variant)",
    )
    parser.add_argument(
        "--extra",
        nargs="*",
        default=[],
        metavar="SPEC",
        help="Extra CI specs to merge (role:machine[:ubuntu])",
    )
    args = parser.parse_args()

    if args.dispatch:
        if args.all or args.roles:
            parser.error("--dispatch is mutually exclusive with --all and positional roles")
        cells = _build_dispatch_matrix(args.dispatch)
    elif args.all:
        if args.roles:
            parser.error("--all is mutually exclusive with positional roles")
        cells = build_test_matrix(list_testable_roles())
    elif args.roles:
        extra = [ci_spec_to_cell(s) for s in args.extra] if args.extra else None
        cells = build_test_matrix(args.roles, extra)
    elif not args.json:
        cells = build_test_matrix(list_testable_roles())
    else:
        extra = [ci_spec_to_cell(s) for s in args.extra] if args.extra else None
        cells = build_test_matrix([], extra)

    if args.json:
        print(json.dumps(cells_to_ci_specs(cells)))
    else:
        for cell in cells:
            print(f"{cell.machine}\t{cell.ubuntu}\t{cell.role}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
