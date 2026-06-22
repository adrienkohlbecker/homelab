#!/usr/bin/env python3
"""Validate Podman systemd unit templates against repo healthcheck conventions."""

from __future__ import annotations

import sys
from pathlib import Path

REQUIRED_SNIPPETS = {
    "ExecStartPre=/bin/rm -f %t/%n.ctr-id": "unit must clear stale cidfiles before start",
    "SyslogIdentifier=%N": "journald entries must carry the systemd unit name",
    "Type=notify": "systemd unit must wait for podman's sdnotify state",
    "NotifyAccess=all": "systemd must accept podman's child-process notifications",
    "--cidfile=%t/%n.ctr-id": "podman unit must write a cidfile for ExecStop/ExecStopPost",
    "--cgroups=split": "podman cgroups must compose with systemd unit accounting",
    "--detach": "podman must detach so systemd tracks readiness through sdnotify",
    "--replace": "podman must replace stale containers after interrupted starts",
    "--rm": "podman must remove stopped containers instead of accumulating state",
    "--log-driver journald": "container logs must flow through journald",
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

    print("Validated Podman service template unit and healthcheck contracts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
