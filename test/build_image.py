#!/usr/bin/env -S uv run
"""Build the homelab:<codename> container images for each configured Ubuntu release.

Iterates over UBUNTU_RELEASES from machine.py so the available codenames stay
in sync with the test harness. Run from the repo root.
"""

import argparse
import subprocess
import sys
from pathlib import Path

from machine import UBUNTU_RELEASES

DOCKERFILE = Path("test/Dockerfile")


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
        print("Error: --ubuntu must list at least one codename", file=sys.stderr)
        return 1
    for codename in codenames:
        if codename not in UBUNTU_RELEASES:
            print(
                f"Error: unknown Ubuntu codename '{codename}'; valid: {sorted(UBUNTU_RELEASES)}",
                file=sys.stderr,
            )
            return 1

    if not DOCKERFILE.exists():
        print(f"Error: {DOCKERFILE} not found (run from the repo root)", file=sys.stderr)
        return 1

    for codename in codenames:
        version = UBUNTU_RELEASES[codename]
        cmd = [
            args.builder, "build",
            "--build-arg", f"UBUNTU_VERSION={version}",
            "--tag", f"homelab:{codename}",
            "-f", str(DOCKERFILE),
            str(DOCKERFILE.parent),
        ]
        print(f"\n==> Building homelab:{codename} (ubuntu:{version})")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            return result.returncode

    return 0


if __name__ == "__main__":
    sys.exit(main())
