#!/usr/bin/env bash
set -euo pipefail

RUN_CHECKMODE=0
KEEP_VM=0
MACHINE=container
QEMU_USE_VNC=0
ROLE=""
PASS_ARGS=()

# Flags:
#   --machine <name>      : machine profile (container|minimal|box|lab|pug)
#   --checkmode           : run playbook with --check and staged tag passes
#   --keep                : leave the container/VM running for inspection
#   --vnc                 : expose QEMU display via VNC (:0) with qxl video
#   --                    : stop parsing and forward the rest to Ansible
while [[ $# -gt 0 ]]; do
  case "$1" in
    --checkmode)
      RUN_CHECKMODE=1
      ;;
    --keep)
      KEEP_VM=1
      ;;
    --vnc)
      QEMU_USE_VNC=1
      ;;
    --machine)
      MACHINE=${2:-}
      shift
      ;;
    --)
      shift
      PASS_ARGS+=("$@")
      break
      ;;
    -*)
      PASS_ARGS+=("$1")
      ;;
    *)
      if [[ -z "$ROLE" ]]; then
        ROLE="$1"
      else
        PASS_ARGS+=("$1")
      fi
      ;;
  esac
  shift
done

if [[ -z "$ROLE" ]]; then
  echo "Usage: $0 [--machine container|minimal|box|lab|pug] [--checkmode] [--keep] [--vnc] <role> [ansible args...]" >&2
  exit 1
fi

SSH_KEY=packer/vagrant.key
SSH_HOST=127.0.0.1
UBUNTU_NAME=jammy
UBUNTU_VERSION=22.04
IDFILE=""
PORT=""
SSH_CMD=""
ANSIBLE_ARGS=""

case $(uname -m) in
aarch64 | arm64)
  UBUNTU_MIRROR="http://ports.ubuntu.com/ubuntu-ports/"
  UBUNTU_MIRROR_SECURITY="http://ports.ubuntu.com/ubuntu-ports/"
  ;;
x86_64)
  UBUNTU_MIRROR="http://archive.ubuntu.com/ubuntu/"
  UBUNTU_MIRROR_SECURITY="http://security.ubuntu.com/ubuntu/"
  ;;
*)
  echo >&2 "Unknown machine name"
  exit 1
  ;;
esac

# Ansible and tooling live in the venv; keep it loaded.
source .venv/bin/activate

if [[ "$MACHINE" == "container" ]]; then
  SSH_USER=root
  ANSIBLE_ARGS="-e {\"docker_test\":true} -e @host_vars/box-podman.yml"
  IDFILE=cid
  INVENTORY_HOST=box

  case $(uname -s) in
  "Darwin")
    IMAGEDIR="$TMPDIR"
    PODMAN="podman"
    ;;
  "Linux")
    IMAGEDIR=/mnt/qemu
    PODMAN="sudo podman"
    ;;
  *)
    echo >&2 "Unknown operating system"
    exit 1
    ;;
  esac
else
  # QEMU: either the full box (OVMF) or the minimal cloud image.
  IMAGEDIR=/mnt/qemu
  IDFILE=pid

  case "$MACHINE" in
    minimal)
      SSH_USER=ubuntu
      ANSIBLE_ARGS="-e {\"qemu_test\":true,\"qemu_test_minimal\":true} -e @host_vars/box-qemu-minimal.yml"
      INVENTORY_HOST=box
      ;;
    box)
      SSH_USER=vagrant
      ANSIBLE_ARGS="-e {\"qemu_test\":true,\"qemu_test_minimal\":false} -e @host_vars/box-qemu.yml"
      INVENTORY_HOST=box
      ;;
    lab)
      SSH_USER=vagrant
      ANSIBLE_ARGS="-e {\"qemu_test\":true,\"qemu_test_minimal\":false} -e @host_vars/lab-qemu.yml"
      INVENTORY_HOST=lab
      ;;
    pug)
      SSH_USER=vagrant
      ANSIBLE_ARGS="-e {\"qemu_test\":true,\"qemu_test_minimal\":false} -e @host_vars/pug-qemu.yml"
      INVENTORY_HOST=pug
      ;;
    *)
      echo >&2 "Unknown machine: $MACHINE"
      exit 1
      ;;
  esac
fi

WORKDIR=$(mktemp --directory --tmpdir=$IMAGEDIR)
TIMEOUT_PID=""

cleanup() {
  if [[ -n "${TIMEOUT_PID:-}" ]]; then
    if (( KEEP_VM )); then
      SSH_CMD="ssh -i $SSH_KEY -p $PORT -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $SSH_USER@$SSH_HOST"
      echo "Keeping VM around, ssh using:"
      echo "> $SSH_CMD"
      if [[ "$MACHINE" == "container" ]]; then
        echo "Then Ctrl+C or"
        echo "> $PODMAN stop --ignore --time 5 --cidfile $WORKDIR/$IDFILE"
        trap '$PODMAN stop --ignore --time 5 --cidfile $WORKDIR/$IDFILE' INT
      else
        echo "Then Ctrl+C or"
        echo "> kill $TIMEOUT_PID"
        trap "kill $TIMEOUT_PID" INT
      fi
    else
      if [[ "$MACHINE" == "container" ]]; then
        $PODMAN stop --ignore --time 5 --cidfile $WORKDIR/$IDFILE
      else
        kill "$TIMEOUT_PID" || true
      fi
    fi
    wait $TIMEOUT_PID || true
  fi

  rm -rf "$WORKDIR"
}
trap cleanup EXIT

cp -r group_vars "$WORKDIR"
cp -r host_vars "$WORKDIR"
cp -r wireguard "$WORKDIR"
cp -r roles "$WORKDIR"

# Minimal playbook to run the single role; inject role _test if present.
cat <<EOF >"$WORKDIR/site.yml"
- hosts: $INVENTORY_HOST
  roles:
    - $ROLE
EOF
if [ -f "roles/$ROLE/tasks/_test.yml" ]; then
  cat <<EOF >>"$WORKDIR/_test.yml"
- hosts: $INVENTORY_HOST
  tasks:
    - import_role:
        name: $ROLE
        tasks_from: _test
EOF
fi

if [[ "$MACHINE" == "container" ]]; then
  $PODMAN network inspect homelab_net >/dev/null || $PODMAN network create --subnet 192.5.0.0/16 homelab_net

  # Start privileged systemd container so roles behave like a VM.
  timeout --kill-after=10s 10m \
    $PODMAN run --interactive --rm --publish 127.0.0.1::22 --privileged --cidfile $WORKDIR/$IDFILE --network homelab_net homelab:$UBUNTU_NAME \
    &
  TIMEOUT_PID=$!

  while [ ! -f $WORKDIR/$IDFILE ]; do
    if ! kill -0 $TIMEOUT_PID &> /dev/null; then
      echo "Launching VM failed"
      exit 1
    fi

    echo -n "."
    sleep 1
  done
  echo "Booted"

  sleep 2

  ADDR=$($PODMAN port "$(cat "$WORKDIR/$IDFILE")" 22)
  PORT="${ADDR#*:}"
else
  QEMU_DRIVES=()
  if (( QEMU_USE_VNC )); then
    QEMU_DISPLAY_ARGS=(-display vnc=:0 -vga qxl)
  else
    QEMU_DISPLAY_ARGS=(-display none)
  fi
  case "$MACHINE" in
    minimal)
      cloud-localds "$WORKDIR/seed.img" test/minimal/user-data test/minimal/meta-data
      qemu-img create -f qcow2 -b "$IMAGEDIR/ubuntu-$UBUNTU_VERSION-minimal-cloudimg-amd64.img" -F qcow2 "$WORKDIR/disk.img" 20G >/dev/null

      QEMU_DRIVES+=(
        -drive "file=$WORKDIR/disk.img,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap"
        -drive "file=$WORKDIR/seed.img,if=virtio,format=raw"
      )
      ;;
    box)
      qemu-img create -f qcow2 -b "$IMAGEDIR/$UBUNTU_NAME/ubuntu-$MACHINE/packer-ubuntu-1" -F qcow2 "$WORKDIR/packer-ubuntu-1" >/dev/null
      cp "$IMAGEDIR/$UBUNTU_NAME/ubuntu-$MACHINE/efivars.fd" "$WORKDIR/efivars.fd"

      QEMU_DRIVES+=(
        -drive "file=$WORKDIR/packer-ubuntu-1,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap"
        -drive "file=/usr/share/OVMF/OVMF_CODE.fd,if=pflash,unit=0,format=raw,readonly=on"
        -drive "file=$WORKDIR/efivars.fd,if=pflash,unit=1,format=raw"
      )
      ;;
    lab)
      qemu-img create -f qcow2 -b "$IMAGEDIR/$UBUNTU_NAME/ubuntu-$MACHINE/packer-ubuntu-1" -F qcow2 "$WORKDIR/packer-ubuntu-1" >/dev/null
      qemu-img create -f qcow2 -b "$IMAGEDIR/$UBUNTU_NAME/ubuntu-$MACHINE/packer-ubuntu-2" -F qcow2 "$WORKDIR/packer-ubuntu-2" >/dev/null
      qemu-img create -f qcow2 -b "$IMAGEDIR/$UBUNTU_NAME/ubuntu-$MACHINE/packer-ubuntu-3" -F qcow2 "$WORKDIR/packer-ubuntu-3" >/dev/null
      qemu-img create -f qcow2 -b "$IMAGEDIR/$UBUNTU_NAME/ubuntu-$MACHINE/packer-ubuntu-4" -F qcow2 "$WORKDIR/packer-ubuntu-4" >/dev/null
      qemu-img create -f qcow2 -b "$IMAGEDIR/$UBUNTU_NAME/ubuntu-$MACHINE/packer-ubuntu-5" -F qcow2 "$WORKDIR/packer-ubuntu-5" >/dev/null
      qemu-img create -f qcow2 -b "$IMAGEDIR/$UBUNTU_NAME/ubuntu-$MACHINE/packer-ubuntu-6" -F qcow2 "$WORKDIR/packer-ubuntu-6" >/dev/null
      qemu-img create -f qcow2 -b "$IMAGEDIR/$UBUNTU_NAME/ubuntu-$MACHINE/packer-ubuntu-7" -F qcow2 "$WORKDIR/packer-ubuntu-7" >/dev/null
      qemu-img create -f qcow2 -b "$IMAGEDIR/$UBUNTU_NAME/ubuntu-$MACHINE/packer-ubuntu-8" -F qcow2 "$WORKDIR/packer-ubuntu-8" >/dev/null
      qemu-img create -f qcow2 -b "$IMAGEDIR/$UBUNTU_NAME/ubuntu-$MACHINE/packer-ubuntu-9" -F qcow2 "$WORKDIR/packer-ubuntu-9" >/dev/null
      cp "$IMAGEDIR/$UBUNTU_NAME/ubuntu-$MACHINE/efivars.fd" "$WORKDIR/efivars.fd"

      QEMU_DRIVES+=(
        -drive "file=$WORKDIR/packer-ubuntu-1,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap"
        -drive "file=$WORKDIR/packer-ubuntu-2,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap"
        -drive "file=$WORKDIR/packer-ubuntu-3,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap"
        -drive "file=$WORKDIR/packer-ubuntu-4,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap"
        -drive "file=$WORKDIR/packer-ubuntu-5,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap"
        -drive "file=$WORKDIR/packer-ubuntu-6,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap"
        -drive "file=$WORKDIR/packer-ubuntu-7,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap"
        -drive "file=$WORKDIR/packer-ubuntu-8,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap"
        -drive "file=$WORKDIR/packer-ubuntu-9,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap"
        -drive "file=/usr/share/OVMF/OVMF_CODE.fd,if=pflash,unit=0,format=raw,readonly=on"
        -drive "file=$WORKDIR/efivars.fd,if=pflash,unit=1,format=raw"
      )
      ;;
    pug)
      qemu-img create -f qcow2 -b "$IMAGEDIR/$UBUNTU_NAME/ubuntu-$MACHINE/packer-ubuntu-1" -F qcow2 "$WORKDIR/packer-ubuntu-1" >/dev/null
      qemu-img create -f qcow2 -b "$IMAGEDIR/$UBUNTU_NAME/ubuntu-$MACHINE/packer-ubuntu-2" -F qcow2 "$WORKDIR/packer-ubuntu-2" >/dev/null
      qemu-img create -f qcow2 -b "$IMAGEDIR/$UBUNTU_NAME/ubuntu-$MACHINE/packer-ubuntu-3" -F qcow2 "$WORKDIR/packer-ubuntu-3" >/dev/null
      cp "$IMAGEDIR/$UBUNTU_NAME/ubuntu-$MACHINE/efivars.fd" "$WORKDIR/efivars.fd"

      QEMU_DRIVES+=(
        -drive "file=$WORKDIR/packer-ubuntu-1,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap"
        -drive "file=$WORKDIR/packer-ubuntu-2,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap"
        -drive "file=$WORKDIR/packer-ubuntu-3,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap"
        -drive "file=/usr/share/OVMF/OVMF_CODE.fd,if=pflash,unit=0,format=raw,readonly=on"
        -drive "file=$WORKDIR/efivars.fd,if=pflash,unit=1,format=raw"
      )
      ;;
    *)
      echo >&2 "Unknown machine: $MACHINE"
      exit 1
      ;;
  esac

  timeout --kill-after=10s 10m \
    qemu-system-x86_64 \
    "${QEMU_DRIVES[@]}" \
    -netdev "user,id=user.0,hostfwd=tcp:$SSH_HOST:0-:22" \
    -object rng-random,id=rng0,filename=/dev/urandom -device virtio-rng-pci,rng=rng0 \
    -machine "type=q35,accel=kvm" \
    -smp "8,sockets=8" \
    -name "packer-ubuntu" \
    -m "4096M" \
    -cpu "host" \
    "${QEMU_DISPLAY_ARGS[@]}" \
    -device "virtio-net,netdev=user.0" \
    -pidfile "$WORKDIR/$IDFILE" \
    &
  TIMEOUT_PID=$!

  while [ ! -f $WORKDIR/$IDFILE ]; do
    if ! kill -0 $TIMEOUT_PID &> /dev/null; then
      echo "Launching VM failed"
      exit 1
    fi

    echo -n "."
    sleep 1
  done
  echo "Booted"

  sleep 2

  PORT=$(lsof -i -P | grep "$(cat "$WORKDIR/$IDFILE")" | grep TCP | cut -d':' -f2 | cut -d' ' -f1 | grep -vE "^59")
fi

while [ -z "$(socat -T2 stdout tcp:127.0.0.1:$PORT,connect-timeout=2,readbytes=1 2>/dev/null)" ]; do
  echo -n "."
  sleep 1
done
echo "SSH up"

mkdir -p test/out
SSH_CMD="ssh -i $SSH_KEY -p $PORT -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $SSH_USER@$SSH_HOST"

# On failure, pull a readable journal.
err() {
  if [ -z "$SSH_CMD" ]; then
    echo "No SSH session available for logs"
    return 0
  fi

  TMPFILE=test/out/$ROLE.journal.ansi
  if [[ "$MACHINE" == "container" ]]; then
    $PODMAN exec --tty "$(cat "$WORKDIR/$IDFILE")" env SYSTEMD_COLORS=true journalctl --pager-end --no-pager --priority info >"$TMPFILE"
  else
    $SSH_CMD env SYSTEMD_COLORS=true journalctl --pager-end --no-pager --priority info >"$TMPFILE"
  fi
  echo "$TMPFILE"
}
trap err ERR

$SSH_CMD sudo bash <<EOF
truncate -s0 /etc/apt/sources.list
echo "deb $UBUNTU_MIRROR $UBUNTU_NAME main restricted universe multiverse" >> /etc/apt/sources.list
echo "deb $UBUNTU_MIRROR $UBUNTU_NAME-updates main restricted universe multiverse" >> /etc/apt/sources.list
echo "deb $UBUNTU_MIRROR_SECURITY $UBUNTU_NAME-security main restricted universe multiverse" >> /etc/apt/sources.list
echo "deb $UBUNTU_MIRROR $UBUNTU_NAME-backports main restricted universe multiverse" >> /etc/apt/sources.list
apt-get update
EOF
if [[ "$MACHINE" == "minimal" ]]; then
  # Fixes systemd-analyze validation error:
  # /lib/systemd/system/snapd.service:23: Unknown key name 'RestartMode' section 'Service', ignoring.
  $SSH_CMD sudo apt-get purge --autoremove --yes snapd
fi

export ANSIBLE_DISPLAY_OK_HOSTS=true
export ANSIBLE_DISPLAY_SKIPPED_HOSTS=true

ANSIBLE_PLAYBOOK="ansible-playbook $ANSIBLE_ARGS -e ansible_ssh_port=$PORT -e ansible_ssh_host=$SSH_HOST -e ansible_ssh_user=$SSH_USER -e ansible_ssh_private_key_file=$SSH_KEY -e ubuntu_mirror=$UBUNTU_MIRROR -e ubuntu_mirror_security=$UBUNTU_MIRROR_SECURITY --inventory test/inventory.ini"

set -x

if [ -f "$WORKDIR/_test.yml" ]; then
  $ANSIBLE_PLAYBOOK "$WORKDIR/_test.yml"
fi

if (( RUN_CHECKMODE )); then
  LIST_TAGS=$(ansible-playbook "$WORKDIR/site.yml" --list-tags)

  $ANSIBLE_PLAYBOOK "$WORKDIR/site.yml" --check "${PASS_ARGS[@]}"

  run_check_stage() {
    local stage=$1
    shift

    if [[ $LIST_TAGS == *"${stage}"* ]]; then
      $ANSIBLE_PLAYBOOK "$WORKDIR/site.yml" --tags "$stage" "$@"
      $ANSIBLE_PLAYBOOK "$WORKDIR/site.yml" --check "$@"
    fi
  }

  for stage in _check_stage1 _check_stage2 _check_stage3 _check_stage4; do
    run_check_stage "$stage" "${PASS_ARGS[@]}"
  done

fi

$ANSIBLE_PLAYBOOK "$WORKDIR/site.yml" "${PASS_ARGS[@]}"
set +x
