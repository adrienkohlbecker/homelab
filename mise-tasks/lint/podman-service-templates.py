#!/usr/bin/env python3
"""Validate Podman systemd unit templates against repo healthcheck conventions."""

from __future__ import annotations

import sys
from pathlib import Path

REQUIRED_SNIPPETS = {
    "Type=notify": "systemd unit must wait for podman's sdnotify state",
    "NotifyAccess=all": "systemd must accept podman's child-process notifications",
    "--sdnotify=healthy": "podman must gate readiness on the container healthcheck",
    "--health-cmd": "container must carry an in-container steady-state healthcheck",
    "--health-startup-cmd": "container must carry a startup healthcheck",
}


def main() -> int:
    errors: list[str] = []
    for path in sorted(Path("roles").glob("*/templates/*.service.j2")):
        text = path.read_text()
        if "podman run" not in text:
            continue

        for snippet, reason in REQUIRED_SNIPPETS.items():
            if snippet not in text:
                errors.append(f"{path}: missing {snippet!r}: {reason}")

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print("Validated Podman service template healthcheck contracts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
