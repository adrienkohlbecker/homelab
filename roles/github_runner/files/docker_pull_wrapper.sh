#!/bin/bash
# Wrapper for /usr/bin/docker installed by the github_runner role.
#
# Why: actions/runner does an unconditional `docker pull <image>` before
# creating a job container. With DOCKER_HOST pointing at podman's
# docker-compat socket, the pull request goes through podman's compat
# API, which (per the Docker API spec) always pulls from a registry --
# no short-circuit to local storage. For our locally-built lab-runtime
# image (auto-tagged `localhost/lab-runtime:latest` by podman build),
# the registry is "localhost" -> connects to nginx on lab -> TLS cert
# mismatch -> pull fails.
#
# podman's CLI pull *does* short-circuit: it respects --policy=missing
# (the default) and returns success if the image is already in local
# storage. So we intercept only `docker pull` here and route it to
# podman directly. All other subcommands (create, start, exec, logs,
# inspect, ...) pass through to /usr/bin/docker (docker-ce-cli) which
# uses DOCKER_HOST as before -- those work fine because the docker-compat
# API for create/start/exec doesn't need the registry round-trip.
set -euo pipefail
if [ "${1:-}" = "pull" ]; then
  shift
  exec /usr/bin/podman pull --policy=missing "$@"
fi
exec /usr/bin/docker "$@"
