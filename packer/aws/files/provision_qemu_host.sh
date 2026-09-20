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

case "$TARGET_ARCHITECTURE" in
x86_64) ;;
aarch64)
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

# passt cannot run confined on these hosts, and neither half of AppArmor can
# be kept on its own. The packaged profile predates this kernel mediating
# AF_UNIX, so accept4() on the qemu socket is denied and the guest never gets
# a network; unload the profile instead and passt dies at startup with
# "unshare: Operation not permitted", because that profile was the only thing
# granting it an exception to apparmor_restrict_unprivileged_userns. Drop
# both. These are single-purpose, disposable CI workers that run our own code,
# and lab — where passt works — already has neither.
sudo install -dm 0755 /etc/apparmor.d/disable
sudo ln -sf /etc/apparmor.d/usr.bin.passt /etc/apparmor.d/disable/usr.bin.passt
sudo apparmor_parser -R /etc/apparmor.d/usr.bin.passt || true
sudo tee /etc/sysctl.d/99-homelab-ci-passt.conf >/dev/null <<'EOF'
# passt sandboxes itself in a user namespace; see the AppArmor note in
# packer/aws/files/provision_qemu_host.sh.
kernel.apparmor_restrict_unprivileged_userns = 0
EOF
sudo sysctl --system >/dev/null

if [ "$TARGET_ARCHITECTURE" = aarch64 ]; then
  # firmware.sh resolves its pins relative to its own location; give it the
  # repository layout it expects.
  firmware_tree=$(mktemp -d)
  install -D -m 0755 /tmp/firmware.sh "$firmware_tree/mise-tasks/test/firmware.sh"
  install -D -m 0644 /tmp/versions.yml "$firmware_tree/group_vars/all/versions.yml"
  install -D -m 0644 /tmp/architectures.yml "$firmware_tree/data/architectures.yml"
  sudo env HOMELAB_AARCH64_FIRMWARE_DIR="$HOMELAB_AARCH64_FIRMWARE_DIR" \
    bash "$firmware_tree/mise-tasks/test/firmware.sh"
  rm -rf "$firmware_tree"
fi

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
  mise_environment=(
    MISE_DATA_DIR=/opt/mise
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

sudo tee /usr/local/bin/homelab_ci_ready >/dev/null <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
for _ in {1..90}; do
  scratch_state=$(systemctl is-active homelab-ci-scratch.service 2>/dev/null || true)
  case "$scratch_state" in
    active) break ;;
    failed | inactive | deactivating) exit 1 ;;
  esac
  sleep 1
done
[ "$scratch_state" = active ]
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

# Hydrate the default Lab image as soon as scratch is up, so the first job on
# a host (often the critical-path site converge) finds it cached instead of
# waiting ~30s for S3. The script's per-image flock makes a job that arrives
# mid-hydration wait and then reuse the result; a failure here only means the
# job hydrates itself. The copy keeps the repository layout the script's
# relative imports and data reads expect; bundle checksums guard content, and
# a job whose newer script rejects this copy's cache simply re-hydrates.
hydrate_root=/opt/homelab-ci/hydrate
sudo install -D -m 0755 /tmp/hydrate-qemu-images.py "$hydrate_root/mise-tasks/ci/hydrate-qemu-images.py"
sudo install -D -m 0644 /tmp/qemu_image_store.py "$hydrate_root/mise-tasks/ci/qemu_image_store.py"
sudo install -D -m 0644 /tmp/matrix.py "$hydrate_root/test/matrix.py"
sudo install -D -m 0644 /tmp/architectures.yml "$hydrate_root/data/architectures.yml"
sudo install -D -m 0644 /tmp/ubuntu_releases.yml "$hydrate_root/data/ubuntu_releases.yml"
sudo tee /etc/systemd/system/homelab-ci-prehydrate.service >/dev/null <<UNIT
[Unit]
Description=Pre-hydrate the default qemu image for homelab CI jobs
Requires=homelab-ci-scratch.service
After=homelab-ci-scratch.service network-online.target
Wants=network-online.target

[Service]
# exec: boot and host readiness proceed while the download runs.
Type=exec
User=ubuntu
Environment=MISE_DATA_DIR=/opt/mise
Environment=PATH=/opt/mise/shims:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/python3 $hydrate_root/mise-tasks/ci/hydrate-qemu-images.py lab
TimeoutStartSec=10min

[Install]
WantedBy=multi-user.target
UNIT
sudo systemd-analyze verify /etc/systemd/system/homelab-ci-prehydrate.service
sudo systemctl enable homelab-ci-prehydrate.service

if [ "$TARGET_ARCHITECTURE" = aarch64 ]; then
  firmware_code="$HOMELAB_AARCH64_FIRMWARE_DIR/edk2-aarch64-code.fd"
  firmware_vars="$HOMELAB_AARCH64_FIRMWARE_DIR/edk2-aarch64-vars.fd"
else
  firmware_code=/usr/share/OVMF/OVMF_CODE_4M.fd
  firmware_vars=/usr/share/OVMF/OVMF_VARS_4M.fd
fi
bash /tmp/qemu_host_smoke.sh kernel
bash /tmp/qemu_host_smoke.sh toolchain
bash /tmp/qemu_host_smoke.sh passt
bash /tmp/qemu_host_smoke.sh firmware "$QEMU_SYSTEM_BINARY" "$QEMU_MACHINE_TYPE" "$firmware_code" "$firmware_vars"

sudo apt-get clean
sudo rm -rf \
  /var/lib/apt/lists/* \
  /tmp/gitlab-runner \
  /tmp/amazon-cloudwatch-agent.deb \
  /tmp/cloudwatch_agent.json \
  /tmp/gitlab_runner_fleeting_arm.pub \
  /tmp/homelab_ci_prepare_scratch.sh \
  /tmp/qemu_host_smoke.sh \
  /tmp/hydrate-qemu-images.py \
  /tmp/qemu_image_store.py \
  /tmp/matrix.py \
  /tmp/ubuntu_releases.yml \
  /tmp/firmware.sh \
  /tmp/versions.yml \
  /tmp/architectures.yml \
  /tmp/homelab-ci-build
