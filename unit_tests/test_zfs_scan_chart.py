"""A collector read failure leaves a gap and recovers without a Netdata restart."""

import subprocess
from pathlib import Path


def test_read_failure_leaves_a_gap_and_recovers():
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
zfs_scan_update 60000000
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
    assert result.stderr.splitlines() == ["zfs_scan: cannot read scan state"]
    assert result.stdout.splitlines() == [
        "BEGIN zfs_scan.any_in_progress 60000000",
        "SET active = 1",
        "END",
        "BEGIN zfs_scan.any_in_progress 60000000",
        "SET active = 0",
        "END",
    ]
