"""Test matrix generation shared by CI and local test runners.

Reads roles/*/meta/test.yml to produce the (machine, ubuntu, role) cell list
that both test/testall.py and mise-tasks/ci/detect.py consume.
"""

import functools
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_UBUNTU_CATALOG = yaml.safe_load((_REPO_ROOT / "data" / "ubuntu_releases.yml").read_text())
UBUNTU_RELEASES: dict[str, str] = {
    codename: release["version"] for codename, release in _UBUNTU_CATALOG["releases"].items()
}
DEFAULT_UBUNTU: str = _UBUNTU_CATALOG["default"]
DEFAULT_MACHINES = ("lab",)

_ROLE_META_KEYS = {"arm", "base_prerequisites", "machines", "skip", "ubuntu"}


class TestCell(NamedTuple):
    """A (machine, ubuntu, role) triple to test."""

    machine: str
    ubuntu: str
    role: str


@dataclass(frozen=True)
class RoleTestConfig:
    """Validated role-test metadata consumed by local and CI matrix builders."""

    base_prerequisites: bool
    machines: tuple[str, ...]
    ubuntu: tuple[str, ...]
    skip: frozenset[tuple[str, str]]
    arm_machines: tuple[str, ...]


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
        return RoleTestConfig(True, DEFAULT_MACHINES, (), frozenset(), ())
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
    machines: list[str] = []
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
            if machine_config not in (None, {}):
                errors.append(f"machines.{name} must be empty")
                continue
            machines.append(name)
    if not machines:
        machines = list(DEFAULT_MACHINES)

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
    skip: set[tuple[str, str]] = set()
    if raw_skip is not None and not isinstance(raw_skip, dict):
        errors.append(f"skip must be a mapping of cell-spec -> reason, got {type(raw_skip).__name__}")
    elif isinstance(raw_skip, dict):
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
            skip.add((machine, codename))

    raw_arm = data.get("arm", [])
    arm_machines: list[str] = []
    if not isinstance(raw_arm, list):
        errors.append(f"arm must be a list, got {type(raw_arm).__name__}")
    else:
        for name in raw_arm:
            if not isinstance(name, str):
                errors.append(f"arm entries must be strings, got {type(name).__name__}")
            elif name not in machines:
                errors.append(f"arm machine {name!r} not in machines {machines}")
            elif (name, DEFAULT_UBUNTU) in skip:
                errors.append(f"arm machine {name!r} skips the default release")
            elif name in arm_machines:
                errors.append(f"duplicate arm machine {name!r}")
            else:
                arm_machines.append(name)

    if errors:
        raise RoleTestConfigError(meta_path, errors)

    return RoleTestConfig(base_prerequisites, tuple(machines), tuple(ubuntu), frozenset(skip), tuple(arm_machines))


def build_role_cells(role: str) -> list[TestCell]:
    """Expand a single role into its test cells.

    - One base cell per machine in machines: (machine, noble, role)
    - Release cell per (machine, ubuntu) cross-product for each ubuntu
      in meta/test.yml: (machine, codename, role).

    Cells listed under skip: are excluded.
    """
    config = load_role_test_config(role)
    machines = config.machines
    skip = config.skip
    cells = [TestCell(m, DEFAULT_UBUNTU, role) for m in machines if (m, DEFAULT_UBUNTU) not in skip]
    for codename in config.ubuntu:
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
        cells.update(c for c in extra_cells if (c.machine, c.ubuntu) not in load_role_test_config(c.role).skip)
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


def build_dispatch_matrix(dispatch_input: str) -> list[TestCell]:
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
