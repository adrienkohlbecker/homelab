#!/bin/bash
set -euo pipefail

unit=${1:-}
if [ "$unit" = "" ]; then
  echo >&2 "Expected unit as argument, got: '$*'"
  exit 1
fi

items="{{ apt_unit_masked_unit|string()|ternary([apt_unit_masked_unit],apt_unit_masked_unit)|join(' ') }}"

found=false
for item in $items; do
  if [ "$unit" = "$item" ] || [ "$unit.service" = "$item" ]; then
    found=true
  fi
done

if [ "$found" = true ]; then
  msg="denied call with [$*]"
  echo >&2 "policy-rc.d $msg"
  logger --priority user.warn --tag policy-rc.d "$msg"
  exit 101
else
  echo >&2 "policy-rc.d called with $*"
fi
