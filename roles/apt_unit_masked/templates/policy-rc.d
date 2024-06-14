#!/bin/bash
set -euo pipefail

unit=${1:-}
if [ -z "$unit" ]; then
  echo "Expected unit as argument, got: '$*'" >&2
  exit 1
fi

items="{{ (apt_unit_masked_unit is string) | ternary([apt_unit_masked_unit], apt_unit_masked_unit) | join(' ') }}"

found=false
for item in $items; do
  if [ "$unit" = "$item" ] || [ "$unit.service" = "$item" ] || [ "$unit" = "$item.service" ]; then
    found=true
  fi
done

if [ "$found" = true ]; then
  msg="denied call with [$*]"
  echo "policy-rc.d $msg" >&2
  logger --priority user.warn --tag policy-rc.d "$msg"
  exit 101
else
  echo "policy-rc.d called with $*" >&2
fi
