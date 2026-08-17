#!/usr/bin/env python3
# [MISE] description="Validate roles/*/meta/test.yml against the harness's MACHINE_CHOICES"
"""Validate role test metadata before CI renders the qemu matrix."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

# Import metadata constants from the test harness so the source of truth stays
# single. test/ isn't a package, so prepend it to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "test"))
from machine import MACHINE_CHOICES
from matrix import DEFAULT_UBUNTU, UBUNTU_RELEASES

MACHINE_NAMES = sorted(MACHINE_CHOICES)
UBUNTU_NAMES = sorted(UBUNTU_RELEASES)
TOP_LEVEL_KEYS = {"machines", "skip", "ubuntu"}


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

        if "machine" in data:
            errors.append(f"{meta}: uses legacy 'machine:' key -- migrate to 'machines:'")
        errors.extend(
            f"{meta}: unknown top-level key {key!r}; expected one of {sorted(TOP_LEVEL_KEYS)}"
            for key in sorted(set(data) - TOP_LEVEL_KEYS)
        )

        if (machines := data.get("machines")) is not None:
            if not isinstance(machines, dict):
                errors.append(f"{meta}: machines must be a mapping, got {type(machines).__name__}")
            else:
                errors.extend(
                    f"{meta}: machines key {name!r} not in {MACHINE_NAMES}"
                    for name in sorted(set(machines) - set(MACHINE_NAMES))
                )
                for name, machine_config in machines.items():
                    if machine_config is not None and not isinstance(machine_config, dict):
                        errors.append(
                            f"{meta}: machines.{name} must be empty or a mapping, "
                            f"got {type(machine_config).__name__}"
                        )

        if (ubuntu := data.get("ubuntu")) is not None:
            if not isinstance(ubuntu, list):
                errors.append(f"{meta}: ubuntu must be a list, got {type(ubuntu).__name__}")
            else:
                for codename in ubuntu:
                    # The default release already has a base cell per machine, so
                    # listing it here expands to nothing. Erroring is what forces
                    # a ubuntu: block to be re-read after a release promotion --
                    # otherwise its rationale silently outlives the cell.
                    if codename == DEFAULT_UBUNTU:
                        errors.append(
                            f"{meta}: ubuntu lists {DEFAULT_UBUNTU!r}, the default release"
                            " -- it expands to no cell, so drop it (list only extra releases)"
                        )
                    elif codename not in UBUNTU_RELEASES:
                        errors.append(f"{meta}: ubuntu={codename!r} not in {UBUNTU_NAMES}")

        # skip maps machine[:ubuntu] to the reason a known-failing cell is quarantined.
        skip = data.get("skip")
        if skip is not None:
            if not isinstance(skip, dict):
                errors.append(f"{meta}: skip must be a mapping of cell-spec -> reason, got {type(skip).__name__}")
            else:
                for spec, reason in skip.items():
                    parts = str(spec).split(":")
                    if len(parts) > 2:
                        errors.append(f"{meta}: skip {spec!r}: too many ':' (want machine or machine:codename)")
                        continue
                    machine = parts[0]
                    codename = parts[1] if len(parts) == 2 else DEFAULT_UBUNTU
                    if machine not in MACHINE_NAMES:
                        errors.append(f"{meta}: skip {spec!r}: machine {machine!r} not in {MACHINE_NAMES}")
                    # `machine:<default>` and bare `machine` name the same cell,
                    # so the explicit spelling reads as "skip the escalation"
                    # while silently cancelling the base cell. Demand the bare
                    # form, which says what it does.
                    if len(parts) == 2 and parts[1] == DEFAULT_UBUNTU:
                        errors.append(
                            f"{meta}: skip {spec!r}: {DEFAULT_UBUNTU!r} is the default release,"
                            f" so this cancels the base cell -- write {machine!r} if that is intended"
                        )
                    elif codename not in UBUNTU_RELEASES:
                        errors.append(f"{meta}: skip {spec!r}: ubuntu {codename!r} not in {UBUNTU_NAMES}")
                    if not reason or not str(reason).strip():
                        errors.append(f"{meta}: skip {spec!r}: needs a non-empty reason")

    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        return 1
    print(f"Validated {len(meta_files)} test.yml file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
