#!/bin/bash

if [ "$1" = "{{ unit }}" ] || [ "$1.service" = "{{ unit }}" ]; then
  msg="denied call with [$*]"
  echo "policy-rc.d $msg" 1>&2
  logger --priority user.warn --tag policy-rc.d "$msg"
  exit 101
else
  echo "policy-rc.d called with $*" 1>&2
fi
