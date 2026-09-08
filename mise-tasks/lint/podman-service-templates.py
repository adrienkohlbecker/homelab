#!/usr/bin/env python3
# [MISE] description="Validate Podman systemd unit healthcheck conventions"
"""Validate Podman systemd unit templates against repo healthcheck conventions."""

import sys
from pathlib import Path

REQUIRED_SNIPPETS = (
    "ExecStartPre=/bin/rm -f %t/%n.ctr-id",
    "SyslogIdentifier=%N",
    "Type=notify",
    "NotifyAccess=all",
    "--cidfile=%t/%n.ctr-id",
    "--cgroups=split",
    "--detach",
    "--replace",
    "--rm",
    "--log-driver journald",
    "--sdnotify=healthy",
    "--health-cmd",
    "--health-startup-cmd",
)


def main() -> int:
    errors: list[str] = []
    for path in sorted(Path("roles").glob("*/templates/*.service.j2")):
        text = path.read_text()
        if "podman run" not in text:
            continue

        errors.extend(f"{path}: missing {snippet!r}" for snippet in REQUIRED_SNIPPETS if snippet not in text)

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print("Validated Podman service template unit and healthcheck contracts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
