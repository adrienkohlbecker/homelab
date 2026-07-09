#!/usr/bin/env bash
#MISE description="Run one role test via test/testrole.py"
#USAGE arg "<role>" help="Role name under roles/"
#USAGE complete "role" run="find roles -mindepth 1 -maxdepth 1 -type d -exec basename {} \\; | sort"
set -euo pipefail

exec test/testrole.py "$@"
