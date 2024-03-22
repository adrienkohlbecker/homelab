#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euo pipefail

while [ $(docker ps --format "{{.Names}}" | wc -l) -gt 0 ]; do
  sleep 2
done
