#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

curl -LX POST https://hub.docker.com/api/build/v1/source/d49cfe2c-0634-46c7-9a85-83daa8fccf4b/trigger/e982769f-b18c-4c49-a2e7-b32980089cef/call/ # sabnzbd
curl -LX POST https://hub.docker.com/api/build/v1/source/3d2ceba3-bad1-484a-b19f-77948c8e4898/trigger/3d49909e-ba3e-4b1e-a61f-fe77f9c28c26/call/ # couchpotato
curl -LX POST https://hub.docker.com/api/build/v1/source/4ceb2b56-9348-44db-9b71-dc1d89f94485/trigger/72082cdc-595b-4a8b-9c96-a4fe7a7ba851/call/ # ssh

dms cebc7586ba
