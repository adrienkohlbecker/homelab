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
# state on disk; the only volume is /work (checkout root, per-instance).
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
#  - git / curl / jq / xz-utils: actions framework helpers + the
#    entrypoint's generate-jitconfig POST + JSON manipulation.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq xz-utils \
      nodejs \
      podman podman-docker \
    && rm -rf /var/lib/apt/lists/*

# actions/runner version pin. Image is the single source of truth for
# the binary version; runners are ephemeral (one job per container,
# JIT-registered) so there's no persisted-state version-skew to
# reconcile. Bumping here ships a new image; next ansible apply pulls
# it and subsequent container starts run the new binary directly out
# of /actions-runner.
ARG ACTIONS_RUNNER_VERSION=2.334.0
ARG ACTIONS_RUNNER_SHA256_X64=048024cd2c848eb6f14d5646d56c13a4def2ae7ee3ad12122bee960c56f3d271
ARG ACTIONS_RUNNER_SHA256_ARM64=f44255bd3e80160eb25f71bc83d06ea025f6908748807a584687b3184759f7e4

# Download + verify + extract into /actions-runner (the canonical
# install dir name actions/runner's own docs use). No bind-mount over
# this path at runtime -- ephemeral runners get binaries from the
# image, never the host.
#
# Extract path is GNU tar + tolerate-then-validate. Two alternatives
# we tried first don't work here:
#  * bsdtar / GNU tar with default flags: chmod on the relative
#    symlinks at ./externals/node{20,24}/bin/{npx,npm,corepack}
#    takes glibc's fchmodat(AT_SYMLINK_NOFOLLOW) shim, which uses
#    /proc/self/fd under the hood -- EPERM in the rootless podman
#    build's userns. tar errors out with rc=2.
#  * python3's tarfile (extractall(filter='data')) -- works fine in
#    a normal env (verified on macOS python 3.11.7 against this
#    exact tarball) but in this rootless podman build it silently
#    extracts nothing while exit=0. Root cause not pinned; not
#    worth chasing when the obvious workaround works.
#
# What works: tar --no-same-owner --no-same-permissions to skip the
# chown the rootless userns can't satisfy and to use umask-derived
# perms instead of trying to restore the archived ones. tar still
# exits rc=2 on the chmod-symlink failures even with those flags
# (the chmod call still happens for the symlink-mode itself), but
# every regular file IS extracted before that error -- including
# run.sh. So: `tar ... || true` to swallow tar's rc, then a `test
# -x /actions-runner/run.sh` to assert the extract actually got
# the entry that matters. If run.sh is missing the test fails the
# build with a clear cause instead of shipping an empty image.
RUN arch=$(dpkg --print-architecture); \
    case "$arch" in \
      amd64) tar_arch=x64; sha=$ACTIONS_RUNNER_SHA256_X64;; \
      arm64) tar_arch=arm64; sha=$ACTIONS_RUNNER_SHA256_ARM64;; \
      *) echo >&2 "Unsupported arch: $arch"; exit 1;; \
    esac; \
    mkdir -p /actions-runner; \
    curl -fsSL -o /tmp/runner.tar.gz \
      "https://github.com/actions/runner/releases/download/v${ACTIONS_RUNNER_VERSION}/actions-runner-linux-${tar_arch}-${ACTIONS_RUNNER_VERSION}.tar.gz"; \
    echo "${sha}  /tmp/runner.tar.gz" | sha256sum -c -; \
    tar xzf /tmp/runner.tar.gz -C /actions-runner \
      --no-same-owner --no-same-permissions \
      || echo >&2 "tar exited non-zero (expected for symlink chmod EPERM); validating below"; \
    test -x /actions-runner/run.sh \
      || { echo >&2 "extract failed: /actions-runner/run.sh missing or not executable"; exit 1; }; \
    test -x /actions-runner/config.sh \
      || { echo >&2 "extract failed: /actions-runner/config.sh missing or not executable"; exit 1; }; \
    rm /tmp/runner.tar.gz

ENV ACTIONS_RUNNER_VERSION=${ACTIONS_RUNNER_VERSION}

# /work = actions/runner's checkout root (job _work tree). systemd unit
# bind-mounts a per-instance host path here so concurrent qcow2 overlays
# + role checkouts can't ENOSPC the container's writable layer. VOLUME
# marks it so a stray `podman run` without --volume gets a clear error
# rather than silently writing to the overlay.
VOLUME ["/work"]

COPY runner/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
