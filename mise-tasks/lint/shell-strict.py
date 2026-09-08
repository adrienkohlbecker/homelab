#!/usr/bin/env python3
# [MISE] description="Validate checked-in shell scripts use strict mode"
"""Require strict mode in checked-in shell entrypoints."""

import fnmatch
import re
import subprocess
import sys
from pathlib import Path

ALLOWLIST_PATTERNS = (
    "mise-tasks/worktree/lib.sh",
    "mise-tasks/zbm/lib.sh",
    "zbm/dracut-modules/89zbm-power-commands/module-setup.sh",
    "roles/netdata/files/*.chart.sh",
    "roles/hdparm/files/*.chart.sh",
    "roles/systemd_timer/files/*.chart.sh",
    "roles/zfs/files/*.chart.sh",
)

SHEBANG_RE = re.compile(r"^#!.*\b(?:ba|z|k)?sh\b")
STRICT_MODE_RE = re.compile(r"^[ \t]*set -[A-Za-z]*e[A-Za-z]*u[A-Za-z]*o[A-Za-z]* pipefail[ \t]*$", re.MULTILINE)


def is_shell_file(path: Path, text: str) -> bool:
    if path.name.endswith((".sh", ".sh.j2")):
        return True
    return bool(SHEBANG_RE.match(text))


def has_strict_mode(text: str) -> bool:
    preamble = "\n".join(text.splitlines()[:40])
    return bool(STRICT_MODE_RE.search(preamble))


def main() -> int:
    errors: list[str] = []
    tracked = (Path(path) for path in subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0") if path)
    for path in tracked:
        if not path.is_file():
            continue
        text = path.read_text(errors="ignore")
        if not is_shell_file(path, text) or any(
            fnmatch.fnmatch(path.as_posix(), pattern) for pattern in ALLOWLIST_PATTERNS
        ):
            continue
        if not has_strict_mode(text):
            errors.append(f"{path}: shell entrypoint must start with set -euo pipefail")

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print("Validated shell strict-mode preambles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
