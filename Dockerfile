# Hosted GitLab CI image, manually published as $CI_REGISTRY_IMAGE/ci:latest.
# Copied manifests invalidate their dependency layers when the toolchain moves.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

# CI builds use Nexus by default; external builds set USE_NEXUS_MIRRORS=0 and
# retain Ubuntu's stock sources. Python dependencies always resolve from the
# indexes pinned in uv.lock.
ARG USE_NEXUS_MIRRORS=1

# Match the repository's amd64 archive/security and arm64 ports split. Nexus
# serves apt over HTTP, while Signed-By still verifies upstream signatures.
# Replacing the stock sources makes mirror failure explicit rather than falling
# back upstream.
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

# Runtime packages support VM disk tests, ZBM key generation, Lua tests, and
# source-built Python wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq xz-utils unzip gpg \
      qemu-system-x86 qemu-utils \
      openssh-client \
      lua5.4 \
      build-essential \
    && rm -rf /var/lib/apt/lists/*

# Docker's repo supplies only the dind client and buildx; Nexus mirrors its URL.
# mise's apt package avoids setgid extraction failures under rootless Buildah.
# The fixed tool and venv paths persist across checkouts without exporting
# VIRTUAL_ENV, leaving uv's explicit environment selection authoritative.
ENV MISE_DATA_DIR=/opt/mise \
    PATH="/opt/venv/bin:/opt/mise/shims:/usr/local/bin:/usr/bin:/bin"
RUN install -dm 755 /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      | gpg --dearmor -o /etc/apt/keyrings/docker-archive-keyring.gpg && \
    curl -fsSL https://mise.jdx.dev/gpg-key.pub \
      | gpg --dearmor -o /etc/apt/keyrings/mise-archive-keyring.gpg && \
    if [ "$USE_NEXUS_MIRRORS" = "1" ]; then \
      docker_repo="https://nexus.lab.fahm.fr/repository/docker-ce"; \
    else \
      docker_repo="https://download.docker.com/linux/ubuntu"; \
    fi && \
    echo "deb [signed-by=/etc/apt/keyrings/docker-archive-keyring.gpg] ${docker_repo} noble stable" \
      > /etc/apt/sources.list.d/docker.list && \
    echo "deb [signed-by=/etc/apt/keyrings/mise-archive-keyring.gpg] https://mise.jdx.dev/deb stable main" \
      > /etc/apt/sources.list.d/mise.list && \
    apt-get update && apt-get install -y --no-install-recommends \
      docker-ce-cli docker-buildx-plugin mise && \
    rm -rf /var/lib/apt/lists/*

# Hosted jobs have fresh HOME values, so use fixed cache and venv paths. The venv
# is shared read-only until lock drift requires copy-up; copy mode avoids noisy
# cross-filesystem hardlink fallback. Disable mise's workspace venv so it cannot
# shadow /opt/venv. Bytecode compilation stays build-only to keep runtime sync a
# no-op instead of recompiling the baked environment in every cell.
ENV UV_CACHE_DIR=/opt/uv-cache \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    MISE_PYTHON_UV_VENV_AUTO=false

# Bake the full mise toolchain and locked Python environment. Build-time
# hardlinks deduplicate the venv against its same-filesystem wheel cache.
WORKDIR /tmp/build
COPY mise.toml pyproject.toml uv.lock ./

# The optional no-scope token raises mise's GitHub rate limit. Mount it only for
# this RUN and expose it only to `mise install`, preventing hooks or caches from
# persisting the credential.
RUN --mount=type=secret,id=mise_github_token \
    mise trust && \
    if [ -s /run/secrets/mise_github_token ]; then \
      GITHUB_TOKEN=$(cat /run/secrets/mise_github_token) mise install; \
    else \
      mise install; \
    fi && \
    UV_COMPILE_BYTECODE=1 mise exec -- uv sync --frozen --link-mode hardlink

# Give shims versions outside a checkout while excluding project env and its
# op:// references.
RUN mkdir -p /etc/mise && \
    awk '/^\[tools\]/{p=1; print; next} /^\[/{p=0} p' /tmp/build/mise.toml \
      > /etc/mise/config.toml

# Pre-install declared Packer plugins into a fixed runtime path because HOME is
# fresh for every hosted job.
ENV PACKER_PLUGIN_PATH=/opt/packer/plugins
COPY packer/qemu.pkr.hcl ./packer/
COPY packer/aws/qemu_host.pkr.hcl ./packer/aws/
RUN mise run packer:init && rm -rf /tmp/build

WORKDIR /
