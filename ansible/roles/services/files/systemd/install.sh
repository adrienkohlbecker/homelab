#!/bin/bash

# Unofficial bash strict mode http://redsymbol.net/articles/unofficial-bash-strict-mode/
set -eu
set -o pipefail
IFS=$'\n\t'

find "$(pwd)" -name "*.service" -exec sh -c 'systemctl is-enabled $(basename $1) | grep "enabled" || systemctl enable $1' _ {} \;
