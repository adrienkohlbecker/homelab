#!/bin/bash
# Wrapper for /usr/bin/docker installed by the github_runner role.
#
# Why: actions/runner does an unconditional `docker pull <image>` before
# creating each job container. With DOCKER_HOST pointing at podman's
# docker-compat socket, that pull goes through podman's compat API which
# (per Docker API spec) always tries the registry -- no short-circuit
# to local storage. Our lab-runtime image lives only in this runner's
# local podman storage (built by the github_runner role; never pushed
# to a registry), so the pull fails -- "localhost" gets parsed as a
# registry host and hits lab's nginx with the wrong TLS cert.
#
# On this self-hosted runner, the only image workflows ever request
# (lab-runtime) is always built locally by the role before any workflow
# fires. So `docker pull` always wants something we already have, and
# the safe simplification is to declare success without doing anything.
# Everything else (create, start, exec, logs, inspect, ...) passes
# through to the real /usr/bin/docker (docker-ce-cli) which talks to
# podman's docker-compat socket via DOCKER_HOST as before -- those
# verbs don't need the registry round-trip and work fine.
set -euo pipefail
if [ "${1:-}" = "pull" ]; then
  echo "github_runner docker wrapper: pull intercepted (image is built locally)"
  exit 0
fi
exec /usr/bin/docker "$@"
