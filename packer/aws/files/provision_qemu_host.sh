#!/usr/bin/env bash
set -euxo pipefail

: "${GITLAB_RUNNER_URL:?gitlab_runner_url is required}"
: "${GITLAB_RUNNER_SHA256:?gitlab_runner_sha256 is required}"

sudo install -dm 755 /etc/apt/keyrings
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
  ca-certificates \
  curl \
  git \
  jq \
  xz-utils \
  unzip \
  gpg \
  gpg-agent \
  apt-transport-https \
  qemu-system-x86 \
  qemu-utils \
  ovmf \
  openssh-client \
  netcat-openbsd \
  passt \
  xorriso \
  cloud-image-utils \
  python3-yaml \
  build-essential \
  zstd \
  tar \
  mdadm \
  ec2-instance-connect

curl -fsSL https://mise.jdx.dev/gpg-key.pub |
  gpg --dearmor |
  sudo tee /etc/apt/keyrings/mise-archive-keyring.gpg >/dev/null
echo 'deb [signed-by=/etc/apt/keyrings/mise-archive-keyring.gpg] https://mise.jdx.dev/deb stable main' |
  sudo tee /etc/apt/sources.list.d/mise.list >/dev/null
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends mise

curl -fsSL -o /tmp/gitlab-runner "$GITLAB_RUNNER_URL"
echo "${GITLAB_RUNNER_SHA256}  /tmp/gitlab-runner" | sha256sum -c -
sudo install -m 0755 -o root -g root /tmp/gitlab-runner /usr/local/bin/gitlab-runner
sudo ln -sf /usr/local/bin/gitlab-runner /usr/bin/gitlab-runner
sudo install -m 0755 -o root -g root /tmp/homelab_ci_hydrate_images /usr/local/bin/homelab_ci_hydrate_images
sudo install -m 0755 -o root -g root /tmp/homelab_ci_prepare_scratch /usr/local/bin/homelab_ci_prepare_scratch
sudo usermod -aG kvm ubuntu

sudo install -dm 0755 /opt/mise /opt/uv-cache /opt/venv /etc/mise /tmp/homelab-ci-build
sudo mv /tmp/mise.toml /tmp/pyproject.toml /tmp/uv.lock /tmp/homelab-ci-build/
(
  cd /tmp/homelab-ci-build
  sudo env MISE_DATA_DIR=/opt/mise PATH=/opt/mise/shims:/usr/local/bin:/usr/bin:/bin \
    mise trust /tmp/homelab-ci-build/mise.toml
  sudo env MISE_DATA_DIR=/opt/mise PATH=/opt/mise/shims:/usr/local/bin:/usr/bin:/bin \
    mise install
  sudo env \
    MISE_DATA_DIR=/opt/mise \
    UV_CACHE_DIR=/opt/uv-cache \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    MISE_PYTHON_UV_VENV_AUTO=false \
    PATH=/opt/venv/bin:/opt/mise/shims:/usr/local/bin:/usr/bin:/bin \
    UV_COMPILE_BYTECODE=1 \
    mise exec -- uv sync --frozen --link-mode hardlink
)
sudo awk '/^\[tools\]/{p=1; print; next} /^\[/{p=0} p' /tmp/homelab-ci-build/mise.toml |
  sudo tee /etc/mise/config.toml >/dev/null
sudo chown -R ubuntu:ubuntu /opt/mise /opt/uv-cache /opt/venv

sudo tee /usr/local/bin/homelab_ci_ready >/dev/null <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[ -c /dev/kvm ]
[ -r /dev/kvm ]
[ -w /dev/kvm ]
[ -w /mnt/scratch/gitlab-runner/builds ]
[ -w /mnt/scratch/homelab_ci ]
env -i PATH=/usr/bin:/bin gitlab-runner --version >/dev/null
command -v qemu-system-x86_64 >/dev/null
command -v qemu-img >/dev/null
command -v passt >/dev/null
command -v mise >/dev/null
EOF
sudo chmod 0755 /usr/local/bin/homelab_ci_ready

sudo tee /etc/systemd/system/homelab-ci-scratch.service >/dev/null <<'EOF'
[Unit]
Description=Format and mount local NVMe scratch for homelab CI qemu host
Before=multi-user.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/homelab_ci_prepare_scratch
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable homelab-ci-scratch.service

sudo apt-get clean
sudo rm -rf \
  /var/lib/apt/lists/* \
  /tmp/gitlab-runner \
  /tmp/homelab_ci_prepare_scratch \
  /tmp/homelab-ci-build
