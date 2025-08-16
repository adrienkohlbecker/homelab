#!/usr/bin/env bash
set -euo pipefail

cat test/out.log | cut -f7- | grep -vE "^0\s+0\s+" | cut -f3 | cut -d" " -f2 | tail -n +2
