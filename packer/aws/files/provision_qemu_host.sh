#!/usr/bin/env bash
set -euxo pipefail

: "${GITLAB_RUNNER_URL:?gitlab_runner_url is required}"
: "${GITLAB_RUNNER_SHA256:?gitlab_runner_sha256 is required}"
: "${TARGET_ARCHITECTURE:?target_architecture is required}"
: "${QEMU_PACKAGES:?qemu_packages is required}"
: "${QEMU_SYSTEM_BINARY:?qemu_system_binary is required}"

case "$TARGET_ARCHITECTURE" in
x86_64) ;;
aarch64)
  : "${AARCH64_FIRMWARE_URL:?aarch64_firmware_url is required}"
  : "${AARCH64_FIRMWARE_SHA256:?aarch64_firmware_sha256 is required}"
  : "${HOMELAB_AARCH64_FIRMWARE_DIR:?aarch64_firmware_dir is required}"
  ;;
*)
  echo "provision_qemu_host: unsupported architecture ${TARGET_ARCHITECTURE}" >&2
  exit 2
  ;;
esac

read -r -a qemu_packages <<<"$QEMU_PACKAGES"

sudo install -dm 755 /etc/apt/keyrings
sudo apt-get update -qq
(
  # mdadm's postinst starts its monitoring timers. Keep the image build quiet;
  # systemd will activate the enabled timers normally when an instance boots.
  printf '#!/bin/sh\nexit 101\n' | sudo tee /usr/sbin/policy-rc.d >/dev/null
  sudo chmod 0755 /usr/sbin/policy-rc.d
  trap 'sudo rm -f /usr/sbin/policy-rc.d' EXIT
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    ca-certificates \
    curl \
    git \
    jq \
    xz-utils \
    unzip \
    gpg \
    gpg-agent \
    "${qemu_packages[@]}" \
    qemu-utils \
    openssh-client \
    netcat-openbsd \
    passt \
    xorriso \
    python3-yaml \
    build-essential \
    zstd \
    mdadm \
    ec2-instance-connect
)

if [ "$TARGET_ARCHITECTURE" = aarch64 ]; then
  firmware_deb=$(mktemp)
  firmware_root=$(mktemp -d)
  curl -fsSL -o "$firmware_deb" "$AARCH64_FIRMWARE_URL"
  echo "${AARCH64_FIRMWARE_SHA256}  ${firmware_deb}" | sha256sum -c -
  dpkg-deb -x "$firmware_deb" "$firmware_root"
  sudo install -dm 0755 "$HOMELAB_AARCH64_FIRMWARE_DIR"
  sudo install -m 0644 \
    "$firmware_root/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd" \
    "$HOMELAB_AARCH64_FIRMWARE_DIR/edk2-aarch64-code.fd"
  sudo install -m 0644 \
    "$firmware_root/usr/share/AAVMF/AAVMF_VARS.fd" \
    "$HOMELAB_AARCH64_FIRMWARE_DIR/edk2-aarch64-vars.fd"
  printf '%s\n' "$AARCH64_FIRMWARE_SHA256" |
    sudo tee "$HOMELAB_AARCH64_FIRMWARE_DIR/archive.sha256" >/dev/null
  sudo chmod 0644 "$HOMELAB_AARCH64_FIRMWARE_DIR/archive.sha256"
  rm -rf "$firmware_deb" "$firmware_root"
fi

curl -fsSL https://mise.en.dev/gpg-key.pub |
  gpg --dearmor |
  sudo tee /etc/apt/keyrings/mise-archive-keyring.gpg >/dev/null
echo 'deb [signed-by=/etc/apt/keyrings/mise-archive-keyring.gpg] https://mise.en.dev/deb stable main' |
  sudo tee /etc/apt/sources.list.d/mise.list >/dev/null
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends mise

curl -fsSL -o /tmp/gitlab-runner "$GITLAB_RUNNER_URL"
echo "${GITLAB_RUNNER_SHA256}  /tmp/gitlab-runner" | sha256sum -c -
sudo install -m 0755 -o root -g root /tmp/gitlab-runner /usr/local/bin/gitlab-runner
sudo ln -sf /usr/local/bin/gitlab-runner /usr/bin/gitlab-runner
sudo install -m 0755 -o root -g root /tmp/homelab_ci_prepare_scratch.sh /usr/local/bin/homelab_ci_prepare_scratch
sudo usermod -aG kvm ubuntu

# ARM metal takes longer than EC2 Instance Connect's 60-second key lifetime to
# reach sshd. Keep a dedicated, non-operator key available for the ARM Fleeting
# connector; x86 continues to use EC2 Instance Connect.
sudo install -dm 0755 /etc/ssh/authorized_keys
sudo install -m 0644 -o root -g root \
  /tmp/gitlab_runner_fleeting_arm.pub \
  /etc/ssh/authorized_keys/ubuntu
sudo tee /etc/ssh/sshd_config.d/70_homelab_ci.conf >/dev/null <<'EOF'
AuthorizedKeysFile .ssh/authorized_keys .ssh/authorized_keys2 /etc/ssh/authorized_keys/%u
EOF
sudo sshd -t

sudo install -dm 0755 /opt/mise /opt/uv-cache /etc/mise /tmp/homelab-ci-build
sudo mv /tmp/mise.toml /tmp/pyproject.toml /tmp/uv.lock /tmp/homelab-ci-build/
(
  cd /tmp/homelab-ci-build
  mise_environment=(
    MISE_DATA_DIR=/opt/mise
    PATH=/opt/mise/shims:/usr/local/bin:/usr/bin:/bin
  )
  if [ -n "$MISE_DISABLE_TOOLS" ]; then
    mise_environment+=("MISE_DISABLE_TOOLS=${MISE_DISABLE_TOOLS}")
  fi
  sudo env "${mise_environment[@]}" mise trust /tmp/homelab-ci-build/mise.toml
  sudo env "${mise_environment[@]}" mise install
  # Warm the persistent uv cache through a project-local environment. The
  # environment is build output and is removed with homelab-ci-build below;
  # concurrent jobs create their own environments from the shared cache.
  sudo env \
    MISE_DATA_DIR=/opt/mise \
    MISE_DISABLE_TOOLS="$MISE_DISABLE_TOOLS" \
    UV_CACHE_DIR=/opt/uv-cache \
    MISE_PYTHON_UV_VENV_AUTO=false \
    PATH=/opt/mise/shims:/usr/local/bin:/usr/bin:/bin \
    mise exec -- uv sync --locked --link-mode hardlink
  sudo env "${mise_environment[@]}" mise exec -- true
)
sudo awk '/^\[tools\]/{p=1; print; next} /^\[/{p=0} p' /tmp/homelab-ci-build/mise.toml |
  sudo tee /etc/mise/config.toml >/dev/null
if [ -n "$MISE_DISABLE_TOOLS" ]; then
  printf '\n[settings]\ndisable_tools = ["%s"]\n' "$MISE_DISABLE_TOOLS" |
    sudo tee -a /etc/mise/config.toml >/dev/null
fi
sudo chown -R ubuntu:ubuntu /opt/mise /opt/uv-cache

sudo tee /usr/local/bin/homelab_ci_ready >/dev/null <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[ -c /dev/kvm ]
[ -r /dev/kvm ]
[ -w /dev/kvm ]
[ -w /mnt/scratch/gitlab-runner/builds ]
[ -w /mnt/scratch/homelab_ci ]
env -i PATH=/usr/bin:/bin gitlab-runner --version >/dev/null
command -v __QEMU_SYSTEM_BINARY__ >/dev/null
command -v qemu-img >/dev/null
command -v passt >/dev/null
command -v mise >/dev/null
EOF
sudo sed -i "s/__QEMU_SYSTEM_BINARY__/${QEMU_SYSTEM_BINARY}/" /usr/local/bin/homelab_ci_ready
if [ "$TARGET_ARCHITECTURE" = aarch64 ]; then
  sudo tee -a /usr/local/bin/homelab_ci_ready >/dev/null <<EOF
test -r ${HOMELAB_AARCH64_FIRMWARE_DIR}/edk2-aarch64-code.fd
test -r ${HOMELAB_AARCH64_FIRMWARE_DIR}/edk2-aarch64-vars.fd
test -r ${HOMELAB_AARCH64_FIRMWARE_DIR}/archive.sha256
EOF
fi
sudo chmod 0755 /usr/local/bin/homelab_ci_ready

sudo tee /etc/systemd/system/homelab-ci-scratch.service >/dev/null <<'EOF'
[Unit]
Description=Format and mount ephemeral scratch for homelab CI qemu host
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
  /tmp/gitlab_runner_fleeting_arm.pub \
  /tmp/homelab_ci_prepare_scratch.sh \
  /tmp/homelab-ci-build
