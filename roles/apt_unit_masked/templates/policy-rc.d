#!/bin/bash

if {% for item in (apt_unit_masked_unit if apt_unit_masked_unit is not string else [apt_unit_masked_unit]) %}[ "$1" = "{{ item }}" ] || [ "$1.service" = "{{ item }}" ]{% if not loop.last %} || {% endif %}{% endfor %}; then
  msg="denied call with [$*]"
  echo "policy-rc.d $msg" 1>&2
  logger --priority user.warn --tag policy-rc.d "$msg"
  exit 101
else
  echo "policy-rc.d called with $*" 1>&2
fi
