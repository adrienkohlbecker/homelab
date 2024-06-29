#!/usr/bin/env bash
set -euo pipefail

find roles -mindepth 3 -maxdepth 3 -wholename "roles/*/tasks/main.yml" -print0 | xargs -0 -n1 dirname | xargs -n1 dirname | xargs -n1 basename | sort | parallel --verbose --jobs 10 --tag --joblog out.log test/testrole.sh {} --checkmode
