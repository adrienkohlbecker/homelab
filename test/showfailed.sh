#!/usr/bin/env bash
set -euo pipefail

cat out.log | grep -vE "0\s+0\s+" | cut -f9 | cut -d" " -f2 | tail -n +2
