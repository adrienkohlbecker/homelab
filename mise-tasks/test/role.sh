#!/usr/bin/env bash
#MISE description="Run one role test via test/testrole.py"
#USAGE arg "<role>" help="Role name under roles/"
#USAGE complete "role" run="find roles -mindepth 1 -maxdepth 1 -type d -exec basename {} \\; | sort"
#USAGE arg "[args]..." help="testrole.py flags (--machine, --keep, --ubuntu, ...); anything testrole.py does not recognise is forwarded to ansible-playbook"
set -euo pipefail

exec test/testrole.py "$@"
