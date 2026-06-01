#!/usr/bin/env python3
"""CI change-detection logic — Python equivalents of detect-roles.sh data transforms.

Pure functions that classify changed files, split the matrix into per-packer
buckets, compute packer source/ubuntu matrices, and propagate release cells.

detect-roles.sh retains the shell implementation; this module provides
testable Python equivalents that can replace the bash+jq inline over time.
"""

import json
import os
import re
import sys
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Path classification regexes
# ---------------------------------------------------------------------------

# Full-universe triggers: a change to any of these can't be attributed to
# specific roles, so the full universe is tested.  Keep in sync with
# FULL_UNIVERSE_PATTERNS in detect-roles.sh.
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

CI_IMAGE_INPUT_PATTERNS: list[str] = [
    r"Dockerfile",
    r"mise\.toml",
    r"pyproject\.toml",
    r"uv\.lock",
    r"packer/qemu\.pkr\.hcl",
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
    packer_changed: bool
    ci_image_changed: bool


def classify_changed_files(
    paths: list[str],
    *,
    is_master_push: bool = False,
) -> ChangeClassification:
    """Classify changed file paths into CI-relevant categories."""
    roles: set[str] = set()
    full_universe: list[str] = []
    packer_changed = False
    ci_image_changed = False

    for path in paths:
        if not path:
            continue
        if FULL_UNIVERSE_RE.match(path):
            full_universe.append(path)
        if PACKER_PATHS_RE.match(path):
            packer_changed = True
        if is_master_push and CI_IMAGE_INPUTS_RE.match(path):
            ci_image_changed = True
        m = ROLE_PATH_RE.match(path)
        if m:
            roles.add(m.group(1))

    return ChangeClassification(
        direct_roles=sorted(roles),
        full_universe_paths=full_universe,
        packer_changed=packer_changed,
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


def split_matrix_buckets(specs: list[str]) -> MatrixBuckets:
    """Split CI spec strings into per-packer-dependency buckets.

    The machine field (second colon-segment) determines the bucket:
      box/box_deps + no release or jammy  ->  jammy
      box/box_deps + noble                ->  noble
      box/box_deps + resolute             ->  resolute
      anything else (minimal/lab/pug)     ->  minimal
    """
    jammy: list[str] = []
    noble: list[str] = []
    resolute: list[str] = []
    minimal: list[str] = []

    for spec in specs:
        parts = spec.split(":")
        machine = parts[1] if len(parts) >= 2 else ""
        release = parts[2] if len(parts) >= 3 else ""

        if machine not in ("box", "box_deps"):
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
    )


# ---------------------------------------------------------------------------
# Packer source/ubuntu matrix
# ---------------------------------------------------------------------------

ALL_PACKER_SOURCES = ["box", "pug", "lab", "hetzner"]
ALL_PACKER_UBUNTU_BOX = ["jammy", "noble", "resolute"]
DEFAULT_PACKER_UBUNTU_EXTRA = ["jammy"]


class PackerSources(NamedTuple):
    all: list[str]
    box: list[str]
    extra: list[str]


def compute_packer_sources(inputs_sources: str = "") -> PackerSources:
    """Compute packer source matrix from dispatch input.

    Empty input returns the full set; otherwise splits on whitespace.
    """
    sources = [s for s in inputs_sources.split() if s] or list(ALL_PACKER_SOURCES)
    return PackerSources(
        all=sources,
        box=[s for s in sources if s == "box"],
        extra=[s for s in sources if s != "box"],
    )


class PackerUbuntu(NamedTuple):
    box: list[str]
    extra: list[str]


def compute_packer_ubuntu(inputs_ubuntu: str = "") -> PackerUbuntu:
    """Compute packer Ubuntu release matrix from dispatch input.

    Pinned release applies to both calls.  Empty returns the defaults:
    box validates all releases, extra stays jammy-only.
    """
    if inputs_ubuntu:
        return PackerUbuntu(box=[inputs_ubuntu], extra=[inputs_ubuntu])
    return PackerUbuntu(
        box=list(ALL_PACKER_UBUNTU_BOX),
        extra=list(DEFAULT_PACKER_UBUNTU_EXTRA),
    )


# ---------------------------------------------------------------------------
# Release-cell propagation
# ---------------------------------------------------------------------------


def propagate_release_cells(
    direct_roles: list[str],
    consumers: dict[str, list[str]],
    default_machines: dict[str, str],
    role_releases: dict[str, list[str]],
    universe: set[str],
) -> list[str]:
    """Propagate release cells from changed roles onto their consumers.

    For each direct role that declares ubuntu releases in meta/test.yml,
    emit ``consumer:machine:codename`` specs for every consumer that
    imports it and is in the testable universe.
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
            machine = default_machines.get(consumer, "box")
            for codename in releases:
                extra.add(f"{consumer}:{machine}:{codename}")

    return sorted(extra)


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------


def _cmd_classify(args: list[str]) -> int:
    is_master = "--master-push" in args
    paths = [line.strip() for line in sys.stdin if line.strip()]
    result = classify_changed_files(paths, is_master_push=is_master)
    print(json.dumps(result._asdict()))
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

    Called by detect-roles.sh's emit() wrapper.  Writes key=value lines to
    $GITHUB_OUTPUT (CI) or stdout (local).
    """
    from argparse import ArgumentParser

    p = ArgumentParser()
    p.add_argument("--matrix", required=True)
    p.add_argument("--packer-changed", default="false")
    p.add_argument("--ci-image-changed", default="false")
    p.add_argument("--inputs-sources", default="")
    p.add_argument("--inputs-ubuntu", default="")
    opts = p.parse_args(args)

    specs = json.loads(opts.matrix)
    matrix_str = json.dumps(specs)
    buckets = split_matrix_buckets(specs)
    packer = compute_packer_sources(opts.inputs_sources)
    ubuntu = compute_packer_ubuntu(opts.inputs_ubuntu)

    pairs = [
        ("matrix", matrix_str),
        ("matrix_jammy", json.dumps(buckets.jammy)),
        ("matrix_noble", json.dumps(buckets.noble)),
        ("matrix_resolute", json.dumps(buckets.resolute)),
        ("matrix_minimal", json.dumps(buckets.minimal)),
        ("packer_changed", opts.packer_changed),
        ("ci_image_changed", opts.ci_image_changed),
        ("packer_sources", json.dumps(packer.all)),
        ("packer_sources_box", json.dumps(packer.box)),
        ("packer_sources_extra", json.dumps(packer.extra)),
        ("packer_ubuntu_box", json.dumps(ubuntu.box)),
        ("packer_ubuntu_extra", json.dumps(ubuntu.extra)),
    ]

    log_parts = [
        f"matrix={matrix_str}",
        f"(jammy={json.dumps(buckets.jammy)}"
        f" noble={json.dumps(buckets.noble)}"
        f" resolute={json.dumps(buckets.resolute)}"
        f" minimal={json.dumps(buckets.minimal)})",
        f"packer_changed={opts.packer_changed}",
        f"ci_image_changed={opts.ci_image_changed}",
        f"packer_sources={json.dumps(packer.all)}"
        f" (box={json.dumps(packer.box)}"
        f" extra={json.dumps(packer.extra)})",
        f"packer_ubuntu=(box={json.dumps(ubuntu.box)}"
        f" extra={json.dumps(ubuntu.extra)})",
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


_COMMANDS = {
    "classify": _cmd_classify,
    "split-buckets": _cmd_split_buckets,
    "packer-sources": _cmd_packer_sources,
    "packer-ubuntu": _cmd_packer_ubuntu,
    "emit": _cmd_emit,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in _COMMANDS:
        cmds = "|".join(_COMMANDS)
        print(f"usage: detect.py <{cmds}> [args...]", file=sys.stderr)
        return 2
    return _COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
