#!/usr/bin/env -S uv run
"""Build the homelab:<codename> container images for each configured Ubuntu release.

Iterates over UBUNTU_RELEASES from machine.py so the available codenames stay
in sync with the test harness. Run from the repo root.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from machine import UBUNTU_RELEASES
from utils import print_line, run_command

DOCKERFILE = Path("test/Dockerfile")


def build_image(codename: str, builder: str = "podman") -> int:
    """Build homelab:<codename> + bake homelab-service:<codename>. Returns exit code."""
    if codename not in UBUNTU_RELEASES:
        raise ValueError(f"Unknown Ubuntu codename '{codename}'; valid: {sorted(UBUNTU_RELEASES)}")
    if not DOCKERFILE.exists():
        raise FileNotFoundError(f"{DOCKERFILE} not found (run from the repo root)")

    version = UBUNTU_RELEASES[codename]
    cmd = [
        builder,
        "build",
        "--build-arg",
        f"UBUNTU_VERSION={version}",
        "--tag",
        f"homelab:{codename}",
        "-f",
        str(DOCKERFILE),
        str(DOCKERFILE.parent),
    ]
    print_line(f"==> Building homelab:{codename} (ubuntu:{version})")
    rc = asyncio.run(run_command(cmd, check=False)).exitcode
    if rc != 0 or builder != "podman":
        return rc

    # Bake the service variant by running the _bake role through the
    # harness itself (--commit captures the post-converge state). Skips
    # automatically when the inputs hash matches the existing image's
    # label, so this is cheap on warm runs.
    bake_cmd = [
        "test/testrole.py",
        "_bake",
        "--ubuntu",
        codename,
        "--no-build-image",
        "--no-checkmode",
        "--no-idempotence",
        "--no-keep-logs",
        "--commit",
        f"homelab-service:{codename}",
    ]
    return asyncio.run(run_command(bake_cmd, check=False)).exitcode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build homelab:<codename> images for the configured Ubuntu releases.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--ubuntu",
        type=str,
        default=",".join(sorted(UBUNTU_RELEASES)),
        metavar="X",
        help="Comma-separated list of Ubuntu codenames to build (defaults to all configured)",
    )
    parser.add_argument(
        "--builder",
        choices=["podman", "docker"],
        default="podman",
        help="Container builder to use",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    codenames = [u.strip() for u in args.ubuntu.split(",") if u.strip()]
    if not codenames:
        print_line("Error: --ubuntu must list at least one codename", error=True)
        return 1
    for codename in codenames:
        if codename not in UBUNTU_RELEASES:
            print_line(
                f"Error: unknown Ubuntu codename '{codename}'; valid: {sorted(UBUNTU_RELEASES)}",
                error=True,
            )
            return 1

    for codename in codenames:
        rc = build_image(codename, builder=args.builder)
        if rc != 0:
            return rc

    return 0


if __name__ == "__main__":
    sys.exit(main())
