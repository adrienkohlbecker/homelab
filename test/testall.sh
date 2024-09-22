#!/usr/bin/env bash
set -euo pipefail

if [[ ${1:-} == "--onlyfailed" ]]; then
  shift

  cat out.log | grep -vE "0\s+0\s+" | cut -f9 | cut -d" " -f2 | tail -n +2 | parallel --verbose --jobs 10 --tag --joblog out.log test/testrole.sh {} --checkmode
else
  find roles -mindepth 3 -maxdepth 3 -wholename "roles/*/tasks/main.yml" -print0 | xargs -0 -n1 dirname | xargs -n1 dirname | xargs -n1 basename | sort | parallel --verbose --jobs 10 --tag --joblog out.log test/testrole.sh {} --checkmode
fi
