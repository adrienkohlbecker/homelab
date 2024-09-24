#!/usr/bin/env bash
set -euo pipefail

ROLE=$1
shift

if [ "$(uname -o)" = "GNU/Linux" ]; then
  PODMAN="sudo podman"
else
  PODMAN="podman"
fi

WORKDIR=$(mktemp -d)
trap 'rm -rf $WORKDIR' EXIT

cp -r group_vars "$WORKDIR"
cp -r host_vars "$WORKDIR"
cp -r wireguard "$WORKDIR"
cp -r "roles/systemd_unit" "$WORKDIR"
cp -r "roles/usergroup_immediate" "$WORKDIR"
cp -r "roles/apt_unit_masked" "$WORKDIR"
cp -r "roles/logrotate" "$WORKDIR"
cp -r "roles/netdata" "$WORKDIR"
cp -r "roles/nginx" "$WORKDIR"
cp -r "roles/cron" "$WORKDIR"
cp -r "roles/zfs_mount" "$WORKDIR"
cp -r "roles/sort_ini" "$WORKDIR"
cp -r "roles/_test" "$WORKDIR"
cp -r "roles/$ROLE" "$WORKDIR"

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

stop() {
  if [ "${KEEPAROUND:-}" = "1" ]; then
    echo "ssh -i packer/vagrant.key -p $PORT root@localhost"
    echo "$PODMAN stop --ignore --time 5 $(cat $WORKDIR/cid)"
  else
    $PODMAN stop --ignore --time 5 --cidfile $WORKDIR/cid
  fi
  rm -rf "$WORKDIR"
}

err() {
  TMPFILE=$(mktemp)
  $PODMAN exec --tty "$(cat $WORKDIR/cid)" journalctl --pager-end --no-pager --priority info >$TMPFILE
  echo "$TMPFILE"
}

trap err ERR
trap stop EXIT

$PODMAN run --interactive --rm --publish 127.0.0.1::22 --detach --privileged --cidfile $WORKDIR/cid --timeout 600 homelab

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

set -x

if [ -f "$WORKDIR/_test.yml" ]; then
  ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$WORKDIR/_test.yml"
fi

if [[ ${1:-} == "--checkmode" ]]; then
  shift

  LIST_TAGS=$(ansible-playbook "$WORKDIR/site.yml" --list-tags)

  ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$WORKDIR/site.yml" --check "$@"

  if [[ $LIST_TAGS == *"_check_stage1"* ]]; then
    ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$WORKDIR/site.yml" --tags _check_stage1 "$@"
    ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$WORKDIR/site.yml" --check "$@"
  fi

  if [[ $LIST_TAGS == *"_check_stage2"* ]]; then
    ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$WORKDIR/site.yml" --tags _check_stage2 "$@"
    ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$WORKDIR/site.yml" --check "$@"
  fi

  if [[ $LIST_TAGS == *"_check_stage3"* ]]; then
    ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$WORKDIR/site.yml" --tags _check_stage3 "$@"
    ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$WORKDIR/site.yml" --check "$@"
  fi

  if [[ $LIST_TAGS == *"_check_stage4"* ]]; then
    ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$WORKDIR/site.yml" --tags _check_stage4 "$@"
    ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$WORKDIR/site.yml" --check "$@"
  fi

fi

ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$WORKDIR/site.yml" "$@"
