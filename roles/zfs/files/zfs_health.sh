#!/bin/bash
set -euo pipefail
exec /opt/zfs/zfs_status.py health
