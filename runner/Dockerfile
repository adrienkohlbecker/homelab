# Container image for the lab's GitHub Actions runners. Built and pushed
# exclusively by .github/workflows/runner-image.yml to
# nexus.lab.fahm.fr/homelab/runner:<sha> + :latest. Consumed by
# roles/github_runner -- one container per registered runner instance,
# scaled by the github_runner_instances dict per host. On a brand-new
# lab with no prior image in nexus, dispatch runner-image.yml once via
# workflow_dispatch before roles/github_runner can deploy.
#
# DooD: the runner container bind-mounts github_runner's rootless podman
# socket at /var/run/docker.sock. Workflows with `container:` blocks
# have their sibling containers spawned on the host podman; workflows
# that omit `container:` (ci-image.yml, runner-image.yml itself) call
# podman CLI directly inside the runner container, which forwards to
# the host socket via the same bind-mount.
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
#  - podman + podman-docker: workflows that omit a `container:` block
#    (ci-image, runner-image) shell out to `docker build` / `podman
#    build`; podman-docker symlinks /usr/bin/docker -> podman. Both
#    forward to the host podman via the bind-mounted socket.
#  - rsync: entrypoint's binary-sync from /runner-base to the /runner
#    volume on first start and after version bumps.
#  - git / curl / jq / xz-utils: actions framework helpers and the
#    entrypoint script.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq xz-utils \
      nodejs \
      podman podman-docker \
      rsync \
    && rm -rf /var/lib/apt/lists/*

# actions/runner version pin. The SHA pair mirrors the equivalent vars
# in roles/github_runner/vars/main.yml; bumping here triggers the
# entrypoint's version sentinel to rsync new binaries into /runner on
# next container start without deregistering the runner.
ARG ACTIONS_RUNNER_VERSION=2.334.0
ARG ACTIONS_RUNNER_SHA256_X64=048024cd2c848eb6f14d5646d56c13a4def2ae7ee3ad12122bee960c56f3d271
ARG ACTIONS_RUNNER_SHA256_ARM64=f44255bd3e80160eb25f71bc83d06ea025f6908748807a584687b3184759f7e4

# Download + verify + extract into /runner-base. At runtime, /runner is
# a bind-mounted host directory; the entrypoint rsyncs /runner-base/ ->
# /runner/ on first start (or version mismatch), preserving state files
# (.runner, .credentials) so the runner doesn't deregister on image
# bumps.
RUN arch=$(dpkg --print-architecture); \
    case "$arch" in \
      amd64) tar_arch=x64; sha=$ACTIONS_RUNNER_SHA256_X64;; \
      arm64) tar_arch=arm64; sha=$ACTIONS_RUNNER_SHA256_ARM64;; \
      *) echo >&2 "Unsupported arch: $arch"; exit 1;; \
    esac; \
    mkdir -p /runner-base; \
    curl -fsSL -o /tmp/runner.tar.gz \
      "https://github.com/actions/runner/releases/download/v${ACTIONS_RUNNER_VERSION}/actions-runner-linux-${tar_arch}-${ACTIONS_RUNNER_VERSION}.tar.gz"; \
    echo "${sha}  /tmp/runner.tar.gz" | sha256sum -c -; \
    tar xzf /tmp/runner.tar.gz -C /runner-base; \
    rm /tmp/runner.tar.gz

ENV ACTIONS_RUNNER_VERSION=${ACTIONS_RUNNER_VERSION}

# /runner = runner state (binaries + .runner + .credentials + _diag);
# /work = actions/runner's checkout root (job _work tree). systemd unit
# bind-mounts host paths to both. VOLUME marks them so a stray
# `podman run` without --volume gets a clear error rather than writing
# into the container's writable layer.
VOLUME ["/runner", "/work"]

COPY runner/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
