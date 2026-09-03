#!/usr/bin/env python3
"""Test matrix generation — single source of truth for CI and local runs.

Reads roles/*/meta/test.yml to produce the (machine, ubuntu, role) cell list
that both test/testall.py and mise-tasks/ci/detect.py consume.

CLI (JSON, for local inspection / tooling):
  python3 test/matrix.py --json --all                        # full universe
  python3 test/matrix.py --json --dispatch "foo,bar:minimal"  # dispatch input
  python3 test/matrix.py --json --extra C1 C2 -- R1 R2       # push path
  python3 test/matrix.py --json                               # empty matrix

Human-readable (for local inspection):
  python3 test/matrix.py            # full universe, tab-separated
  python3 test/matrix.py foo bar    # specific roles
"""

import argparse
import functools
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_UBUNTU_CATALOG = yaml.safe_load((_REPO_ROOT / "data" / "ubuntu_releases.yml").read_text())
UBUNTU_RELEASES: dict[str, str] = _UBUNTU_CATALOG["releases"]
DEFAULT_UBUNTU: str = _UBUNTU_CATALOG["default"]
DEFAULT_MACHINES = {"box": None}

# Machines that only run on demand (`testrole.py --machine`) and the nightly
# on-lab packer regression -- never in a CI-generated matrix. Their multi-disk
# prod-faithful qemu images are not promoted to S3, so the qemu CI cells (which
# hydrate box/box_deps bundles) cannot boot them. detect drops them from every
# generated pipeline; local testall.py keeps them.
ON_DEMAND_MACHINES = frozenset({"lab", "pug"})
_ROLE_META_KEYS = {"base_prerequisites", "machines", "skip", "ubuntu"}


class TestCell(NamedTuple):
    """A (machine, ubuntu, role) triple to test."""

    machine: str
    ubuntu: str
    role: str


@dataclass(frozen=True)
class RoleTestConfig:
    """Validated role-test metadata consumed by local and CI matrix builders."""

    base_prerequisites: bool
    machines: Mapping[str, dict | None]
    ubuntu: tuple[str, ...]
    skip: Mapping[object, object]


class RoleTestConfigError(ValueError):
    """One role metadata file failed schema validation."""

    def __init__(self, path: Path, messages: list[str]) -> None:
        self.messages = tuple(f"{path}: {message}" for message in messages)
        super().__init__("\n".join(self.messages))


def list_testable_roles() -> list[str]:
    """Return all roles with tasks/main.yml, sorted."""
    roles_dir = Path("roles")
    if not roles_dir.exists():
        return []
    return [d.name for d in sorted(roles_dir.iterdir()) if d.is_dir() and (d / "tasks" / "main.yml").exists()]


def load_role_test_config(role: str, machine_names: tuple[str, ...] = ()) -> RoleTestConfig:
    """Load and validate one role's cached test metadata."""

    meta_path = Path(f"roles/{role}/meta/test.yml").resolve()
    return _load_role_test_config(meta_path, machine_names)


@functools.cache
def _load_role_test_config(meta_path: Path, machine_names: tuple[str, ...]) -> RoleTestConfig:
    """Parse one absolute metadata path once per process."""

    if not meta_path.exists():
        return RoleTestConfig(True, dict(DEFAULT_MACHINES), (), {})
    try:
        data = yaml.safe_load(meta_path.read_text()) or {}
    except yaml.YAMLError as e:
        raise RoleTestConfigError(meta_path, [f"parse error: {e}"]) from e

    if not isinstance(data, dict):
        raise RoleTestConfigError(meta_path, [f"top-level must be a mapping, got {type(data).__name__}"])

    errors: list[str] = []
    if "machine" in data:
        errors.append("uses legacy 'machine:' key -- migrate to 'machines:'")
    errors.extend(
        f"unknown top-level key {key!r}; expected one of {sorted(_ROLE_META_KEYS)}"
        for key in sorted(set(data) - _ROLE_META_KEYS)
    )

    raw_base_prerequisites = data.get("base_prerequisites", True)
    if isinstance(raw_base_prerequisites, bool):
        base_prerequisites = raw_base_prerequisites
    else:
        errors.append(f"base_prerequisites must be a boolean, got {type(raw_base_prerequisites).__name__}")
        base_prerequisites = True

    raw_machines = data.get("machines")
    machines: dict[str, dict | None] = {}
    if raw_machines is None:
        pass
    elif not isinstance(raw_machines, dict):
        errors.append(f"machines must be a mapping, got {type(raw_machines).__name__}")
    else:
        for name, machine_config in raw_machines.items():
            if not isinstance(name, str):
                errors.append(f"machines key must be a string, got {type(name).__name__}")
                continue
            if machine_names and name not in machine_names:
                errors.append(f"machines key {name!r} not in {list(machine_names)}")
            if machine_config is not None and not isinstance(machine_config, dict):
                errors.append(f"machines.{name} must be empty or a mapping, got {type(machine_config).__name__}")
                continue
            machines[name] = machine_config
    if not machines:
        machines = dict(DEFAULT_MACHINES)

    raw_ubuntu = data.get("ubuntu")
    ubuntu: list[str] = []
    if raw_ubuntu is None:
        pass
    elif not isinstance(raw_ubuntu, list):
        errors.append(f"ubuntu must be a list, got {type(raw_ubuntu).__name__}")
    else:
        for codename in raw_ubuntu:
            if not isinstance(codename, str):
                errors.append(f"ubuntu entries must be strings, got {type(codename).__name__}")
            elif codename == DEFAULT_UBUNTU:
                errors.append(
                    f"ubuntu lists {DEFAULT_UBUNTU!r}, the default release"
                    " -- it expands to no cell, so drop it (list only extra releases)"
                )
            elif codename not in UBUNTU_RELEASES:
                errors.append(f"ubuntu={codename!r} not in {sorted(UBUNTU_RELEASES)}")
            else:
                ubuntu.append(codename)

    raw_skip = data.get("skip")
    if raw_skip is None:
        skip = {}
    elif not isinstance(raw_skip, dict):
        errors.append(f"skip must be a mapping of cell-spec -> reason, got {type(raw_skip).__name__}")
        skip = {}
    else:
        skip = raw_skip
        for spec, reason in raw_skip.items():
            parts = str(spec).split(":")
            if len(parts) > 2:
                errors.append(f"skip {spec!r}: too many ':' (want machine or machine:codename)")
                continue
            machine = parts[0]
            codename = parts[1] if len(parts) == 2 else DEFAULT_UBUNTU
            if machine_names and machine not in machine_names:
                errors.append(f"skip {spec!r}: machine {machine!r} not in {list(machine_names)}")
            if len(parts) == 2 and codename == DEFAULT_UBUNTU:
                errors.append(
                    f"skip {spec!r}: {DEFAULT_UBUNTU!r} is the default release,"
                    f" so this cancels the base cell -- write {machine!r} if that is intended"
                )
            elif codename not in UBUNTU_RELEASES:
                errors.append(f"skip {spec!r}: ubuntu {codename!r} not in {sorted(UBUNTU_RELEASES)}")
            if not reason or not str(reason).strip():
                errors.append(f"skip {spec!r}: needs a non-empty reason")

    if errors:
        raise RoleTestConfigError(meta_path, errors)

    return RoleTestConfig(base_prerequisites, machines, tuple(ubuntu), skip)


def machines_for(role: str) -> dict:
    """Test machines from meta/test.yml (falls back to {'box': None}).

    Returns the machines: dict.  First key is the primary machine (used
    for release cells); additional keys get only a base cell.
    """
    return dict(load_role_test_config(role).machines)


def default_machine_for(role: str) -> str:
    """Primary test machine — first key in machines: (falls back to 'box')."""
    return next(iter(machines_for(role)))


def release_ubuntu_for(role: str) -> list[str]:
    """Extra Ubuntu releases from meta/test.yml (empty when none)."""
    return list(load_role_test_config(role).ubuntu)


def base_prerequisites_for(role: str) -> bool:
    """Whether hostname and Apt prerequisites should run before this role."""
    return load_role_test_config(role).base_prerequisites


def skip_for(role: str) -> set[tuple[str, str]]:
    """(machine, ubuntu) cells this role declares as skipped in CI.

    meta/test.yml `skip:` is a mapping of cell-spec -> reason, where a
    cell-spec is `machine` (the noble cell) or `machine:codename` (a
    release cell). Skipped cells are dropped from every generated matrix
    (CI detect and testall), so they can't gate a green run. They are NOT
    a substitute for a fix -- `testrole.py <role> --machine <m>` still
    runs a skipped cell directly (it bypasses this matrix), which is how
    you iterate on the fix that lets the skip be removed.
    """
    skip = load_role_test_config(role).skip
    out: set[tuple[str, str]] = set()
    for spec in skip:
        parts = str(spec).split(":")
        machine = parts[0]
        ubuntu = parts[1] if len(parts) > 1 else DEFAULT_UBUNTU
        out.add((machine, ubuntu))
    return out


def drop_on_demand_cells(specs: list[str]) -> tuple[list[str], list[str]]:
    """Partition CI specs into (kept, dropped) by ON_DEMAND_MACHINES.

    Called only by the CI child-pipeline generator (detect._emit_gitlab); local
    testall.py keeps every machine. lab/pug cells cannot run on either qemu CI
    target (their images are not promoted to S3), so they are dropped from every
    generated pipeline -- the caller logs the drop so it never reads as
    "covered". Exercise them with `testrole.py <role> --machine {lab,pug}`.
    """
    kept: list[str] = []
    dropped: list[str] = []
    for spec in specs:
        if ci_spec_to_cell(spec).machine in ON_DEMAND_MACHINES:
            dropped.append(spec)
        else:
            kept.append(spec)
    return kept, dropped


def build_role_cells(role: str) -> list[TestCell]:
    """Expand a single role into its test cells.

    - One base cell per machine in machines: (machine, noble, role)
    - Release cell per (machine, ubuntu) cross-product for each ubuntu
      in meta/test.yml: (machine, codename, role).

    Cells listed under skip: are excluded.
    """
    machines = machines_for(role)
    skip = skip_for(role)
    cells = [TestCell(m, DEFAULT_UBUNTU, role) for m in machines if (m, DEFAULT_UBUNTU) not in skip]
    for codename in release_ubuntu_for(role):
        # Already emitted as a base cell above; lint/test-meta.py rejects the
        # entry outright, this just keeps the expansion duplicate-free.
        if codename == DEFAULT_UBUNTU:
            continue
        cells.extend(TestCell(m, codename, role) for m in machines if (m, codename) not in skip)
    return cells


def build_test_matrix(
    roles: list[str],
    extra_cells: list[TestCell] | None = None,
) -> list[TestCell]:
    """Build the deduplicated, sorted test matrix for the given roles.

    extra_cells: additional cells to merge (used by CI's release-cell
    propagation from changed helper roles to their consumers).
    """
    cells: set[TestCell] = set()
    for role in roles:
        cells.update(build_role_cells(role))
    if extra_cells:
        # Honour skip: for propagated cells too (a consumer's release cell
        # pushed in via CI's helper-fan-out must still drop if skipped).
        cells.update(c for c in extra_cells if (c.machine, c.ubuntu) not in skip_for(c.role))
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

    Tokens without colons are expanded via build_role_cells (with machine
    + release escalation). Tokens with colons are exact CI specs (no
    escalation — the user said what they wanted).
    """
    universe = set(list_testable_roles())
    cells: list[TestCell] = []
    for token in dispatch_input.split(","):
        token = token.strip()
        if not token:
            continue
        role = token.split(":")[0]
        if role not in universe:
            print(
                f"error: role '{role}' is not in the testable universe (no roles/{role}/tasks/main.yml)",
                file=sys.stderr,
            )
            sys.exit(1)
        if ":" in token:
            cells.append(ci_spec_to_cell(token))
        else:
            cells.extend(build_role_cells(token))
    return cells


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the test matrix from roles/*/meta/test.yml",
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
