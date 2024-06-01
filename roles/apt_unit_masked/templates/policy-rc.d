#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

unit=${1:-}
if [ -z "$unit" ]; then
  f_fail "Expected unit as argument, got: '$*'"
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
  f_error "policy-rc.d $msg"
  logger --priority user.warn --tag policy-rc.d "$msg"
  exit 101
else
  f_error "policy-rc.d called with $*"
fi
