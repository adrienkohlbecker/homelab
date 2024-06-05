#!/bin/bash
set -euo pipefail

SESSION_ID=$(curl --location --silent --connect-timeout 1 --max-time 5 --head --request GET localhost:9091/transmission/rpc | grep 'X-Transmission-Session-Id: ' | sed 's/\r//')
curl --location --fail --silent --show-error --connect-timeout 1 --max-time 5 --output /dev/null --request POST --header "$SESSION_ID" --header "Content-Type: application/json" --data '{"arguments":{"fields":["version"]},"method":"session-get"}' http://localhost:9091/transmission/rpc
