# Container image consumed by every github-actions workflow in this
# repo. Built and pushed exclusively by the ci-image workflow
# to the homelab nexus docker hosted repo at
# nexus.lab.fahm.fr/homelab/ci:latest. Other
# workflows reference that URL directly in their `container:` block;
# anonymous pulls are enabled on Nexus so the runner just fetches it.
# On a brand-new lab runner with no prior image in nexus, kick the
# ci-image workflow once via `workflow_dispatch` before any
# other workflow can succeed. Layered FROM ubuntu:24.04 with everything
# the test harness, lint, and packer-build need pre-installed so jobs
# cold-start in ~3s.
#
# Build inputs (mise.toml / pyproject.toml / uv.lock /
# packer/qemu.pkr.hcl) are COPYed in so a bump to any of them
# invalidates the dependency layers and triggers a fresh `mise
# install` + `uv sync` + `packer init`. Build context root is the repo
# root; the workflow uses actions/checkout's workspace directly.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

# Build-time toggle for the homelab nexus apt mirror redirect below.
# Default 1 = route apt through nexus.lab.fahm.fr because that's the CI
# path. Set to 0 for builds outside the lab network:
# `podman build --build-arg USE_NEXUS_MIRRORS=0 .`. When off, the noble
# image's stock /etc/apt/sources.list.d/ubuntu.sources stays put. pip +
# uv always resolve against PyPI regardless of this toggle (see the note
# below the apt block for why they're not proxied).
ARG USE_NEXUS_MIRRORS=1

# Route apt through the homelab nexus proxy. Same arch split as
# group_vars/all.yml's mirror_apt_ubuntu_* vars: amd64 hits ubuntu-archive
# + ubuntu-security; arm64 hits ubuntu-ports for both (ports.ubuntu.com
# carries -security on non-x86 arches). HTTP because nexus's apt
# proxies serve plaintext on port 80; the upstream Signed-By trust
# chain is unchanged so apt still verifies package signatures end-to-end.
# Overwriting /etc/apt/sources.list.d/ubuntu.sources (the deb822 file
# noble ships) means the upstream URIs are gone for the rest of the
# build -- if nexus is unreachable, apt-get fails loudly here rather
# than silently fanning out to upstream. printf (vs heredoc) so the
# whole conditional fits one \-continuation RUN.
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

# pip + uv are intentionally NOT routed through the nexus pypi proxy
# (unlike apt above). uv.lock pins each package's resolved index URL, so
# resolving against nexus.lab.fahm.fr rewrites those URLs to
# nexus.lab.fahm.fr/... and `uv sync --locked` then fails against a
# lockfile authored on dev machines via PyPI -- same package versions,
# different URLs (the diff is URL/hash fields only). Devs author the lock
# against PyPI, so CI must resolve against PyPI too or --locked can never
# pass. uv's wheel cache is pre-warmed below, so dropping the proxy costs
# no per-run network fetch in the steady state. (pip is unused -- uv is
# the package manager -- so no /etc/pip.conf either.)

# Harness needs qemu-system-x86 + qemu-utils for booting test VMs;
# openssh-client for talking to the guests; xorriso + cloud-image-utils
# for the `minimal` variant's seed iso; python3-yaml so mise-tasks/ci
# scripts can run without uv. build-essential is for any wheel that
# needs to compile. gpg + apt-transport-https are for the mise apt repo.
# nodejs because every actions/checkout / actions/upload-artifact / etc.
# is a JS action and crun aborts with "executable file 'node' not found"
# without it.
#
# curl + netcat-openbsd back the WAN-side delegate_to: localhost probes
# in roles/iptables/tasks/_verify.yml — TCP via curl, UDP via `nc -u`.
# Without netcat, the wireguard ingress probe fails with
# "nc: command not found".
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq xz-utils unzip gpg apt-transport-https \
      qemu-system-x86 qemu-utils ovmf \
      openssh-client coreutils \
      netcat-openbsd \
      xorriso cloud-image-utils \
      python3-yaml \
      nodejs \
      build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install mise via its apt repo — bypasses the tar-with-setgid-bit issue
# that the curl|sh installer hits under rootless podman build (the buildah
# userns doesn't allow tar to preserve those bits, even for files mise
# owns). MISE_DATA_DIR holds the tool tree (python, opentofu, packer, uv,
# shellcheck, ...) so a re-pull of the image doesn't re-download tools.
ENV MISE_DATA_DIR=/opt/mise \
    PATH="/opt/mise/shims:/usr/local/bin:/usr/bin:/bin"
RUN install -dm 755 /etc/apt/keyrings && \
    curl -fsSL https://mise.jdx.dev/gpg-key.pub \
      | gpg --dearmor -o /etc/apt/keyrings/mise-archive-keyring.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/mise-archive-keyring.gpg] https://mise.jdx.dev/deb stable main" \
      > /etc/apt/sources.list.d/mise.list && \
    apt-get update && apt-get install -y --no-install-recommends mise && \
    rm -rf /var/lib/apt/lists/*

# Pin uv's cache to a fixed absolute path instead of the default
# $HOME/.cache/uv. GitHub Actions overrides HOME to /github/home inside
# job containers (same reason PACKER_PLUGIN_PATH is pinned below), so a
# cache warmed at build time under root's home would never be consulted
# at runtime -- every `uv sync` would re-download from the index. Pinning
# here means the warm-up below and every workflow `uv sync` share
# /opt/uv-cache. UV_LINK_MODE=copy because the per-checkout .venv lands on
# the bind-mounted GITHUB_WORKSPACE (a different filesystem than the
# cache), so uv's default hardlink fails and falls back to copy with a
# warning anyway -- this just makes the copy the deliberate path.
ENV UV_CACHE_DIR=/opt/uv-cache \
    UV_LINK_MODE=copy

# Pre-resolve everything mise.toml asks for (python 3.14, uv, terraform,
# packer, shellcheck, shfmt, tflint), then warm uv's wheel cache
# by syncing the dependency tree once. The resulting .venv is discarded
# because it's tied to /tmp/build and per-checkout venvs at workflow
# time will be built against the actual GITHUB_WORKSPACE checkout — but
# the cached wheels in /opt/uv-cache stay and make those `uv sync` calls
# resolve in seconds instead of minutes.
WORKDIR /tmp/build
COPY mise.toml pyproject.toml uv.lock ./

# The mise_github_token build secret raises mise's GitHub API rate
# limit (mise pulls tool releases from gh; anonymous is 60/hr,
# authenticated is 5000/hr). Forwarded by ci-image.yml from
# the MISE_GITHUB_TOKEN repo secret (a long-lived PAT with no scopes
# beyond public read). Mounted only for this RUN -- never lands in any
# image layer.
#
# GITHUB_TOKEN is scoped to the single `mise install` invocation via
# inline-env rather than `export`ed for the whole RUN: a mise plugin
# post-install hook that dumps env into a cached file under
# MISE_DATA_DIR (/opt/mise) or UV_CACHE_DIR (/opt/uv-cache) would
# otherwise bake the token into the layer. mise trust is a local file
# op and uv sync doesn't need GitHub auth, so both run without the
# token visible.
RUN --mount=type=secret,id=mise_github_token \
    mise trust && \
    if [ -s /run/secrets/mise_github_token ]; then \
      GITHUB_TOKEN=$(cat /run/secrets/mise_github_token) mise install; \
    else \
      mise install; \
    fi && \
    mise exec -- uv sync --frozen && \
    rm -rf /tmp/build/.venv

# Extract just the [tools] section into a global config. mise shims
# (uv, opentofu, etc.) can then resolve their version from any CWD;
# without this, running the image with no project-local mise.toml in
# CWD leaves shims with "No version is set". Skipping [env] keeps the
# op:// references from polluting every shell.
RUN mkdir -p /etc/mise && \
    awk '/^\[tools\]/{p=1; print; next} /^\[/{p=0} p' /tmp/build/mise.toml \
      > /etc/mise/config.toml

# Pre-install the packer plugins declared in packer/qemu.pkr.hcl
# (qemu + external) so the packer-build workflow doesn't have to
# fetch them on every CI run -- the container's HOME is fresh each
# time. PACKER_PLUGIN_PATH is honored both during this init and at
# runtime, so the same /opt/packer/plugins tree is found by `packer
# build` later.
ENV PACKER_PLUGIN_PATH=/opt/packer/plugins
COPY packer/qemu.pkr.hcl ./packer/qemu.pkr.hcl
RUN mise run packer:init && rm -rf ./packer

WORKDIR /
