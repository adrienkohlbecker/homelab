# Container image for the lab's GitHub Actions runners. Built and pushed
# exclusively by .github/workflows/runner-image.yml to
# nexus.lab.fahm.fr/homelab/runner:<sha> + :latest. Consumed by
# roles/github_runner -- one container per registered runner instance,
# scaled by the github_runner_instances dict per host. On a brand-new
# lab with no prior image in nexus, dispatch runner-image.yml once via
# workflow_dispatch before roles/github_runner can deploy.
#
# Ephemeral + JIT lifecycle: every container start runs the entrypoint,
# which mints a single-use JIT config blob by POSTing to GitHub's
# /actions/runners/generate-jitconfig with the host's PAT, then execs
# actions/runner's run.sh --jitconfig <blob>. The runner registers,
# grabs one job, runs it, deregisters itself, and exits. systemd's
# Restart=always brings up the next container. No persistent runner
# state on disk inside the container.
#
# This image is OS + tooling only -- the actions/runner binaries live
# on the HOST at /opt/actions-runner (installed by roles/github_runner
# via the actions/runner tarball), bind-mounted into the container at
# the same path. Decoupling the runner version from the image version
# means a runner bump is an ansible-only change (edit
# roles/github_runner/vars/main.yml's URL+sha256 and re-apply); the
# image rebuild via runner-image.yml is only needed for tooling
# changes (apt deps, entrypoint logic, base image bump).
#
# Path-mirroring rationale: bind-mounting /opt/actions-runner at the
# same path on both sides is what makes `container:` workflows work
# over DooD. actions/runner spawns docker create with
# -v /opt/actions-runner/externals:/__e:ro for the workflow container,
# and docker (resolving the source on the host via the bind-mounted
# socket) needs that path to exist on the host. See
# roles/github_runner/templates/github_runner.service.j2's --volume
# comment block for the full story.
#
# DooD: the runner container bind-mounts github_runner's rootless podman
# socket at /var/run/docker.sock. Workflow steps that call podman or
# docker directly are routed at the socket via CONTAINER_HOST /
# DOCKER_HOST env vars (set in the unit), so builds happen on the
# host's rootless podman where mount-ns operations have the privileges
# they need (the runner container itself doesn't have CAP_SYS_ADMIN).
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

# Same nexus-mirror toggle as the ci Dockerfile; see that file's full
# rationale. Default 1 = route through nexus.lab.fahm.fr (CI path); set
# to 0 for builds outside the lab network.
ARG USE_NEXUS_MIRRORS=1
RUN if [ "$USE_NEXUS_MIRRORS" = "1" ]; then \
      arch=$(dpkg --print-architecture); \
      if [ "$arch" = "amd64" ]; then \
        archive_url="http://nexus.lab.fahm.fr/repository/ubuntu-archive"; \
        security_url="http://nexus.lab.fahm.fr/repository/ubuntu-security"; \
      else \
        archive_url="http://nexus.lab.fahm.fr/repository/ubuntu-ports"; \
        security_url="http://nexus.lab.fahm.fr/repository/ubuntu-ports"; \
      fi; \
      printf 'Types: deb\nURIs: %s\nSuites: noble noble-updates noble-backports\nComponents: main restricted universe multiverse\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n\nTypes: deb\nURIs: %s\nSuites: noble-security\nComponents: main restricted universe multiverse\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n' \
        "$archive_url" "$security_url" \
        > /etc/apt/sources.list.d/ubuntu.sources; \
    fi

# Runtime deps:
#  - nodejs: every JS action (actions/checkout, actions/upload-artifact, ...)
#    is invoked via `node` by actions/runner; without it crun aborts with
#    "executable file 'node' not found".
#  - podman + podman-docker: workflow steps that shell out to podman or
#    docker. Both go to the host's rootless podman via the bind-mounted
#    socket + CONTAINER_HOST/DOCKER_HOST env (set in the unit). The
#    in-container podman binary is just the client; no nested rootless
#    setup happens here (would fail anyway, no CAP_SYS_ADMIN).
#  - git / curl / jq / xz-utils: actions framework helpers + the
#    entrypoint's generate-jitconfig POST + JSON manipulation.
#  - procps: pgrep used by the systemd unit's --health-cmd to probe
#    Runner.Listener.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq xz-utils \
      nodejs \
      podman podman-docker \
      procps \
    && rm -rf /var/lib/apt/lists/*

COPY runner/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
