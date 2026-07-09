#!/usr/bin/env bash
#MISE description="Run the full role-test matrix via test/testall.py"
set -euo pipefail

exec test/testall.py "$@"
