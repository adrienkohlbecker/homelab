"""Exercise the calendar period command used by the systemd_timer role."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


def test_calendar_period_parser_handles_long_output(tmp_path: Path) -> None:
    tasks = yaml.safe_load((Path(__file__).resolve().parents[1] / "roles/systemd_timer/tasks/install.yml").read_text())
    script = next(task["shell"] for task in tasks if "Derive overdue period" in task["name"])

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemd_analyze = fake_bin / "systemd-analyze"
    systemd_analyze.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nprintf '%s\\n' 'Next elapse: first' '#2: second'\nseq 1 100000\n"
    )
    systemd_analyze.chmod(0o755)
    date = fake_bin / "date"
    date.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'case "$2" in\n'
        "  first) echo 100000 ;;\n"
        "  second) echo 186400 ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    date.chmod(0o755)

    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "ON_CALENDAR": "daily"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "86400"
