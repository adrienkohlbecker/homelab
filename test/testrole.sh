#!/usr/bin/env bash
set -euo pipefail

ROLE=$1
shift

source .venv/bin/activate

IMAGEDIR=/mnt/scratch/qemu
SSH_USER=root
SSH_KEY=packer/vagrant.key
SSH_HOST=127.0.0.1
UBUNTU_NAME=trusty

if [ "$(uname -o)" = "GNU/Linux" ]; then
  PODMAN="sudo podman"
else
  PODMAN="podman"
fi

WORKDIR=$(mktemp --directory --tmpdir=$IMAGEDIR)
trap 'rm -rf $WORKDIR' EXIT

cp -r group_vars "$WORKDIR"
cp -r host_vars "$WORKDIR"
cp -r wireguard "$WORKDIR"
cp -r roles "$WORKDIR"

cat <<EOF >"$WORKDIR/site.yml"
- hosts: box
  roles:
    - $ROLE
EOF
if [ -f "roles/$ROLE/tasks/_test.yml" ]; then
  cat <<EOF >>"$WORKDIR/_test.yml"
- hosts: box
  tasks:
    - import_role:
        name: $ROLE
        tasks_from: _test
EOF
fi

SSH_CMD="ssh -i $SSH_KEY -p $PORT -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $SSH_USER@$SSH_HOST"

stop() {
  if [ "${KEEPAROUND:-}" = "1" ]; then
    echo "Keeping VM around, ssh using:"
    echo "> $SSH_CMD"
    echo "Then Ctrl+C or"
    echo "> $PODMAN stop --ignore --time 5 --cidfile $WORKDIR/cid"
  else
    $PODMAN stop --ignore --time 5 --cidfile $WORKDIR/cid
  fi
  rm -rf "$WORKDIR"
}

err() {
  TMPFILE=test/out/$ROLE.journal.ansi
  $PODMAN exec --tty "$(cat $WORKDIR/cid)" env SYSTEMD_COLORS=true journalctl --pager-end --no-pager --priority info >$TMPFILE
  echo "$TMPFILE"
}

trap err ERR
trap stop EXIT

# run once: podman network create --subnet 192.5.0.0/16 homelab_net
$PODMAN run --interactive --rm --publish 127.0.0.1::22 --detach --privileged --cidfile $WORKDIR/cid --timeout 600 --network homelab_net homelab

while [ ! -f $WORKDIR/cid ]; do
  echo "."
  sleep 1
done

sleep 2

ADDR=$($PODMAN port $(cat $WORKDIR/cid) 22)
PORT="${ADDR#*:}"

while [ -z "$(socat -T2 stdout tcp:127.0.0.1:$PORT,connect-timeout=2,readbytes=1 2>/dev/null)" ]; do
  echo "."
  sleep 1
done

ANSIBLE_PLAYBOOK="ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT -e ansible_ssh_host=$SSH_HOST -e ansible_ssh_user=$SSH_USER -e ansible_ssh_private_key_file=$SSH_KEY -e "@host_vars/box-podman.yml" --inventory test/inventory.ini"

set -x

if [ -f "$WORKDIR/_test.yml" ]; then
  $ANSIBLE_PLAYBOOK "$WORKDIR/_test.yml"
fi

if [[ ${1:-} == "--checkmode" ]]; then
  shift

  LIST_TAGS=$(ansible-playbook "$WORKDIR/site.yml" --list-tags)

  $ANSIBLE_PLAYBOOK "$WORKDIR/site.yml" --check "$@"

  if [[ $LIST_TAGS == *"_check_stage1"* ]]; then
    $ANSIBLE_PLAYBOOK "$WORKDIR/site.yml" --tags _check_stage1 "$@"
    $ANSIBLE_PLAYBOOK "$WORKDIR/site.yml" --check "$@"
  fi

  if [[ $LIST_TAGS == *"_check_stage2"* ]]; then
    $ANSIBLE_PLAYBOOK "$WORKDIR/site.yml" --tags _check_stage2 "$@"
    $ANSIBLE_PLAYBOOK "$WORKDIR/site.yml" --check "$@"
  fi

  if [[ $LIST_TAGS == *"_check_stage3"* ]]; then
    $ANSIBLE_PLAYBOOK "$WORKDIR/site.yml" --tags _check_stage3 "$@"
    $ANSIBLE_PLAYBOOK "$WORKDIR/site.yml" --check "$@"
  fi

  if [[ $LIST_TAGS == *"_check_stage4"* ]]; then
    $ANSIBLE_PLAYBOOK "$WORKDIR/site.yml" --tags _check_stage4 "$@"
    $ANSIBLE_PLAYBOOK "$WORKDIR/site.yml" --check "$@"
  fi

fi

$ANSIBLE_PLAYBOOK "$WORKDIR/site.yml" "$@"
