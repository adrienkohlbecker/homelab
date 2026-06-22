# Container image for the hosted GitLab jobs in .gitlab-ci.yml. The manual
# ci_image job publishes it to this project's registry as
# $CI_REGISTRY_IMAGE/ci:latest. It layers ubuntu:24.04 with the test harness,
# lint, and packer-build toolchain preinstalled so hosted jobs cold-start fast.
#
# Build inputs (mise.toml / pyproject.toml / uv.lock /
# packer/qemu.pkr.hcl) are COPYed in so a bump to any of them
# invalidates the dependency layers and triggers a fresh `mise
# install` + `uv sync` + `packer init`. Build context root is the repo
# root; the ci_image job builds from the checked-out repository directly.
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
#
# curl + netcat-openbsd back the WAN-side delegate_to: localhost probes
# in roles/iptables/tasks/_verify.yml — TCP via curl, UDP via `nc -u`.
# Without netcat, the wireguard ingress probe fails with
# "nc: command not found".
#
# passt backs the guest NIC over a unix socket (qemu `-netdev stream`,
# present in this image's qemu 8.2) instead of qemu's libslirp, whose
# single-threaded userspace stack drops UDP under load and flakes
# external-DNS _verify. test/machine.py probes for passt + stream support
# and only uses it when both are present, so a jammy host (qemu 6.2, no
# passt) or macOS transparently keeps the slirp path. See
# notes/ci_qemu_net_passt_migration.md.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq xz-utils unzip gpg gpg-agent apt-transport-https \
      qemu-system-x86 qemu-utils ovmf \
      openssh-client coreutils \
      netcat-openbsd \
      passt \
      xorriso cloud-image-utils \
      python3-yaml \
      build-essential \
    && rm -rf /var/lib/apt/lists/*

# docker CLI + buildx plugin for the GitLab zbm_build job, whose scripts
# drive docker directly against the pipeline's dind service. From Docker's
# own apt repo — noble's docker.io would drag in the full daemon. Repo URL
# mirror-gated like the base sources above (group_vars mirror_apt_docker
# shape); the signing key comes from download.docker.com either way — the
# lab build host reaches it fine, it is one tiny fetch, and the Signed-By
# trust chain stays upstream.
RUN install -dm 755 /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      | gpg --dearmor -o /etc/apt/keyrings/docker-archive-keyring.gpg && \
    if [ "$USE_NEXUS_MIRRORS" = "1" ]; then \
      docker_repo="https://nexus.lab.fahm.fr/repository/docker-ce"; \
    else \
      docker_repo="https://download.docker.com/linux/ubuntu"; \
    fi && \
    echo "deb [signed-by=/etc/apt/keyrings/docker-archive-keyring.gpg] ${docker_repo} noble stable" \
      > /etc/apt/sources.list.d/docker.list && \
    apt-get update && apt-get install -y --no-install-recommends \
      docker-ce-cli docker-buildx-plugin && \
    rm -rf /var/lib/apt/lists/*

# Install mise via its apt repo — bypasses the tar-with-setgid-bit issue
# that the curl|sh installer hits under rootless podman build (the buildah
# userns doesn't allow tar to preserve those bits, even for files mise
# owns). MISE_DATA_DIR holds the tool tree (python, opentofu, packer, uv,
# shellcheck, ...) so a re-pull of the image doesn't re-download tools.
#
# /opt/venv/bin is on PATH so the baked uv venv's console scripts
# (ansible-lint, ruff, black, yamllint, pytest, ...) resolve directly:
# MISE_PYTHON_UV_VENV_AUTO=false (set below) stops mise from activating a
# workspace venv, so nothing else puts the venv on PATH. This only prepends
# the bin dir — it does NOT export VIRTUAL_ENV, so uv still selects its
# environment via UV_PROJECT_ENVIRONMENT and the no-shadowing intent holds.
ENV MISE_DATA_DIR=/opt/mise \
    PATH="/opt/venv/bin:/opt/mise/shims:/usr/local/bin:/usr/bin:/bin"
RUN install -dm 755 /etc/apt/keyrings && \
    curl -fsSL https://mise.jdx.dev/gpg-key.pub \
      | gpg --dearmor -o /etc/apt/keyrings/mise-archive-keyring.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/mise-archive-keyring.gpg] https://mise.jdx.dev/deb stable main" \
      > /etc/apt/sources.list.d/mise.list && \
    apt-get update && apt-get install -y --no-install-recommends mise && \
    rm -rf /var/lib/apt/lists/*

# Pin uv's cache to a fixed absolute path instead of the default
# $HOME/.cache/uv. Hosted jobs may run with a fresh HOME, so a cache warmed
# under root's home would be missed at runtime. Pinning here means the warm-up
# below and every job `uv sync` share /opt/uv-cache. UV_LINK_MODE=copy because
# checkout workspaces land on a different filesystem than the cache, so uv's
# default hardlink would warn and fall back to copying anyway.
# Bake the resolved venv into the image and reuse it at runtime, instead of
# rebuilding a per-checkout .venv in every job. The 60-wide CI fan-out
# otherwise rebuilds this venv 60x concurrently, and copy-mode materialization
# of ansible/boto3/awscli is CPU-bound -- it dominated the orchestrator's
# start-burst (uv pegging cores). UV_PROJECT_ENVIRONMENT pins the venv to
# /opt/venv (a read-only image layer shared by every job via the overlay), so a
# cell's `uv sync --locked` is a no-op verify when the checkout's lock matches
# the image, and copies-up + drift-heals only when it actually moved.
# MISE_PYTHON_UV_VENV_AUTO=false stops mise from auto-creating/activating a
# workspace ./.venv that would shadow /opt/venv (it exports VIRTUAL_ENV, which
# uv prefers over UV_PROJECT_ENVIRONMENT). This is the image env only -- local
# dev keeps uv_venv_auto.
#
# UV_COMPILE_BYTECODE is deliberately NOT set here: it's a per-RUN var on the
# build `uv sync` below, not a persistent env. As a persistent env it would
# fire on every runtime `uv sync --locked` too, recompiling all ~18k .pyc into
# the venv (~1.7s of pure CPU) on each of the 60 concurrent cells -- a needless
# slice of the start-burst, since the bake already compiled them and the paths
# (/opt/venv) and interpreter are identical at runtime. Scoped to the build, the
# baked .pyc are reused as-is and the runtime sync stays a 1ms resolve + no-op.
ENV UV_CACHE_DIR=/opt/uv-cache \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    MISE_PYTHON_UV_VENV_AUTO=false

# Pre-resolve everything mise.toml asks for (python 3.14, uv, terraform,
# packer, shellcheck, shfmt, tflint), then build the dependency tree once
# into /opt/venv (UV_PROJECT_ENVIRONMENT above). Both the wheel cache
# (/opt/uv-cache) and the resolved venv (/opt/venv) ship in the image:
# runtime `uv sync --locked` no-ops against the baked venv when the lock
# matches, and the cache covers the drift-heal when it doesn't. --link-mode
# hardlink (overriding the global copy) dedupes /opt/venv against
# /opt/uv-cache -- same filesystem at build time -- so baking the venv
# barely grows the image.
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
    UV_COMPILE_BYTECODE=1 mise exec -- uv sync --frozen --link-mode hardlink

# Extract just the [tools] section into a global config. mise shims
# (uv, opentofu, etc.) can then resolve their version from any CWD;
# without this, running the image with no project-local mise.toml in
# CWD leaves shims with "No version is set". Skipping [env] keeps the
# op:// references from polluting every shell.
RUN mkdir -p /etc/mise && \
    awk '/^\[tools\]/{p=1; print; next} /^\[/{p=0} p' /tmp/build/mise.toml \
      > /etc/mise/config.toml

# Pre-install the packer plugins declared in packer/*.pkr.hcl
# (qemu + external from qemu.pkr.hcl, aws + ansible from ami.pkr.hcl)
# so workflows don't have to fetch them on every CI run -- the
# container's HOME is fresh each time. PACKER_PLUGIN_PATH is honored
# both during this init and at runtime, so the same /opt/packer/plugins
# tree is found by `packer build` later.
ENV PACKER_PLUGIN_PATH=/opt/packer/plugins
COPY packer/qemu.pkr.hcl ./packer/
COPY packer/aws/qemu_host.pkr.hcl ./packer/aws/
RUN mise run packer:init && rm -rf ./packer

WORKDIR /
