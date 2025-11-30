#!/usr/bin/env bash
set -euo pipefail

PASS_ARGS=("$@")
: "${ROLE:?ROLE is required}"

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
}
trap cleanup EXIT


if [[ "$MACHINE" == "container" ]]; then
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
  timeout --kill-after=10s 10m \
    qemu-system-x86_64 \
    $QEMU_DRIVES \
    -netdev "user,id=user.0,hostfwd=tcp:$SSH_HOST:0-:22" \
    -object rng-random,id=rng0,filename=/dev/urandom -device virtio-rng-pci,rng=rng0 \
    -machine "type=q35,accel=kvm" \
    -smp "8,sockets=8" \
    -name "packer-ubuntu" \
    -m "4096M" \
    -cpu "host" \
    $QEMU_DISPLAY_ARGS \
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
