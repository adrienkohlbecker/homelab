#!/usr/bin/env bash
set -euxo pipefail

: "${GITLAB_RUNNER_URL:?gitlab_runner_url is required}"
: "${GITLAB_RUNNER_SHA256:?gitlab_runner_sha256 is required}"
: "${CLOUDWATCH_AGENT_URL:?cloudwatch_agent_url is required}"
: "${CLOUDWATCH_AGENT_SHA256:?cloudwatch_agent_sha256 is required}"
: "${TARGET_ARCHITECTURE:?target_architecture is required}"
: "${QEMU_PACKAGES:?qemu_packages is required}"
: "${QEMU_SYSTEM_BINARY:?qemu_system_binary is required}"
: "${QEMU_MACHINE_TYPE:?qemu_machine_type is required}"
: "${QEMU_FIRMWARE_CODE:?qemu_firmware_code is required}"
: "${QEMU_FIRMWARE_VARS:?qemu_firmware_vars is required}"
: "${PREHYDRATE_UBUNTU:?prehydrate_ubuntu is required}"

case "$TARGET_ARCHITECTURE" in
x86_64 | aarch64) ;;
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
    xorriso \
    python3-yaml \
    build-essential \
    zstd \
    mdadm \
    ec2-instance-connect
)

curl -fsSL --retry 5 --retry-all-errors --retry-connrefused https://mise.en.dev/gpg-key.pub |
  gpg --dearmor |
  sudo tee /etc/apt/keyrings/mise-archive-keyring.gpg >/dev/null
echo 'deb [signed-by=/etc/apt/keyrings/mise-archive-keyring.gpg] https://mise.en.dev/deb stable main' |
  sudo tee /etc/apt/sources.list.d/mise.list >/dev/null
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends mise

curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/gitlab-runner "$GITLAB_RUNNER_URL"
echo "${GITLAB_RUNNER_SHA256}  /tmp/gitlab-runner" | sha256sum -c -
sudo install -m 0755 -o root -g root /tmp/gitlab-runner /usr/local/bin/gitlab-runner
sudo ln -sf /usr/local/bin/gitlab-runner /usr/bin/gitlab-runner
sudo install -m 0755 -o root -g root /tmp/homelab_ci_prepare_scratch.sh /usr/local/bin/homelab_ci_prepare_scratch
sudo usermod -aG kvm ubuntu

# Host memory, swap, and CPU metrics for capacity tuning. fetch-config only
# translates (and so validates) the config; the enabled unit starts the agent
# on each instance boot, never on the builder.
curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/amazon-cloudwatch-agent.deb "$CLOUDWATCH_AGENT_URL"
echo "${CLOUDWATCH_AGENT_SHA256}  /tmp/amazon-cloudwatch-agent.deb" | sha256sum -c -
sudo DEBIAN_FRONTEND=noninteractive dpkg -i /tmp/amazon-cloudwatch-agent.deb
sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config -m ec2 -c file:/tmp/cloudwatch_agent.json
sudo systemctl enable amazon-cloudwatch-agent.service

# ARM metal takes longer than EC2 Instance Connect's 60-second key lifetime to
# reach sshd. Keep a dedicated, non-operator key available for the ARM Fleeting
# connector; x86 continues to use EC2 Instance Connect.
if [ "$TARGET_ARCHITECTURE" = aarch64 ]; then
  sudo install -dm 0755 /etc/ssh/authorized_keys
  sudo install -m 0644 -o root -g root \
    /tmp/gitlab_runner_fleeting_arm.pub \
    /etc/ssh/authorized_keys/ubuntu
  sudo tee /etc/ssh/sshd_config.d/70_homelab_ci.conf >/dev/null <<'EOF'
AuthorizedKeysFile .ssh/authorized_keys .ssh/authorized_keys2 /etc/ssh/authorized_keys/%u
EOF
  sudo sshd -t
fi

sudo install -dm 0755 /opt/mise /opt/uv-cache /etc/mise /tmp/homelab-ci-build
sudo mv /tmp/mise.toml /tmp/pyproject.toml /tmp/uv.lock /tmp/homelab-ci-build/
(
  cd /tmp/homelab-ci-build
  # mise install runs the project postinstall hook, whose uv sync fills the
  # environment; pointing it at the shared cache keeps that from leaving the
  # warm-up below nothing to fetch into /opt/uv-cache.
  mise_environment=(
    MISE_DATA_DIR=/opt/mise
    UV_CACHE_DIR=/opt/uv-cache
    PATH=/opt/mise/shims:/usr/local/bin:/usr/bin:/bin
  )
  sudo env "${mise_environment[@]}" mise trust /tmp/homelab-ci-build/mise.toml
  sudo env "${mise_environment[@]}" mise install
  # Warm the persistent uv cache through a project-local environment. The
  # environment is build output and is removed with homelab-ci-build below;
  # concurrent jobs create their own environments from the shared cache.
  sudo env \
    MISE_DATA_DIR=/opt/mise \
    UV_CACHE_DIR=/opt/uv-cache \
    MISE_PYTHON_UV_VENV_AUTO=false \
    PATH=/opt/mise/shims:/usr/local/bin:/usr/bin:/bin \
    mise exec -- uv sync --locked --link-mode hardlink
  sudo env "${mise_environment[@]}" mise exec -- true
)
sudo awk '/^\[tools\]/{p=1; print; next} /^\[/{p=0} p' /tmp/homelab-ci-build/mise.toml |
  sudo tee /etc/mise/config.toml >/dev/null
sudo chown -R ubuntu:ubuntu /opt/mise /opt/uv-cache

sudo install -m 0755 -o root -g root /tmp/homelab_ci_ready.sh /usr/local/bin/homelab_ci_ready
sudo install -m 0644 -o root -g root /tmp/homelab-ci-scratch.service /etc/systemd/system/
sudo systemctl enable homelab-ci-scratch.service

# Hydrate the default Lab image as soon as scratch is up, so the first job on
# a host (often the critical-path site converge) finds it cached instead of
# waiting ~30s for S3. The script's per-image flock makes a job that arrives
# mid-hydration wait and then reuse the result; a failure here only means the
# job hydrates itself. The copy is just the script and its store module;
# bundle checksums guard content, and a job whose newer script rejects this
# copy's cache simply re-hydrates.
hydrate_root=/opt/homelab-ci/hydrate
sudo install -D -m 0755 /tmp/hydrate-qemu-images.py "$hydrate_root/hydrate-qemu-images.py"
sudo install -D -m 0644 /tmp/qemu_image_store.py "$hydrate_root/qemu_image_store.py"
sudo install -m 0644 -o root -g root /tmp/homelab-ci-prehydrate@.service /etc/systemd/system/
sudo systemd-analyze verify "/etc/systemd/system/homelab-ci-prehydrate@${PREHYDRATE_UBUNTU}.service"
sudo systemctl enable "homelab-ci-prehydrate@${PREHYDRATE_UBUNTU}.service"

bash /tmp/qemu_host_smoke.sh kernel
bash /tmp/qemu_host_smoke.sh toolchain
bash /tmp/qemu_host_smoke.sh firmware "$QEMU_SYSTEM_BINARY" "$QEMU_MACHINE_TYPE" "$QEMU_FIRMWARE_CODE" "$QEMU_FIRMWARE_VARS"

sudo apt-get clean
sudo rm -rf \
  /var/lib/apt/lists/* \
  /tmp/gitlab-runner \
  /tmp/amazon-cloudwatch-agent.deb \
  /tmp/cloudwatch_agent.json \
  /tmp/gitlab_runner_fleeting_arm.pub \
  /tmp/homelab_ci_prepare_scratch.sh \
  /tmp/homelab_ci_ready.sh \
  /tmp/homelab-ci-scratch.service \
  /tmp/homelab-ci-prehydrate@.service \
  /tmp/qemu_host_smoke.sh \
  /tmp/hydrate-qemu-images.py \
  /tmp/qemu_image_store.py \
  /tmp/homelab-ci-build
