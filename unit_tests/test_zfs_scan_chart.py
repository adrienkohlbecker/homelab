"""Collector read failures leave gaps and recover without a Netdata restart."""

import subprocess
from pathlib import Path


def test_repeated_read_failures_keep_retrying_and_recover():
    chart = Path(__file__).resolve().parents[1] / "roles/zfs/files/zfs_scan.chart.sh"
    result = subprocess.run(
        [
            "bash",
            "-c",
            """
set -euo pipefail
source "$1"
error() { echo >&2 "$*"; }
/opt/zfs/zfs_status.py() { return 2; }
for ((attempt = 0; attempt < 12; attempt++)); do
  zfs_scan_update 60000000
done
/opt/zfs/zfs_status.py() { echo 1; }
zfs_scan_update 60000000
/opt/zfs/zfs_status.py() { echo 0; }
zfs_scan_update 60000000
""",
            "bash",
            str(chart),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stderr.splitlines() == ["zfs_scan: cannot read scan state"] * 12
    assert result.stdout.splitlines() == [
        "BEGIN zfs_scan.any_in_progress 60000000",
        "SET active = 1",
        "END",
        "BEGIN zfs_scan.any_in_progress 60000000",
        "SET active = 0",
        "END",
    ]
