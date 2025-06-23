#!/bin/bash
set -euo pipefail

echo "**** installing ffmpeg ****"
apk add --no-cache ffmpeg git
