#!/usr/bin/env bash
set -euo pipefail

ROLE=$1
shift

TMPDIR=$(mktemp -d)
trap 'rm -rf $TMPDIR' EXIT

cp -r group_vars "$TMPDIR"
cp -r host_vars "$TMPDIR"
cp -r wireguard "$TMPDIR"
cp -r "roles/systemd_unit" "$TMPDIR"
cp -r "roles/usergroup_immediate" "$TMPDIR"
cp -r "roles/apt_unit_masked" "$TMPDIR"
cp -r "roles/logrotate" "$TMPDIR"
cp -r "roles/netdata" "$TMPDIR"
cp -r "roles/nginx" "$TMPDIR"
cp -r "roles/cron" "$TMPDIR"
cp -r "roles/zfs_mount" "$TMPDIR"
cp -r "roles/sort_ini" "$TMPDIR"
cp -r "roles/_test" "$TMPDIR"
cp -r "roles/$ROLE" "$TMPDIR"

cat <<EOF >"$TMPDIR/site.yml"
- hosts: box
  roles:
    - $ROLE
EOF
if [ -f "roles/$ROLE/tasks/_test.yml" ]; then
  cat <<EOF >>"$TMPDIR/_test.yml"
- hosts: box
  tasks:
    - import_role:
        name: $ROLE
        tasks_from: _test
EOF
fi

stop() {
  podman stop --ignore --time 5 --cidfile $TMPDIR/cid
  rm -rf "$TMPDIR"
}

err() {
  TMPFILE=$(mktemp)
  podman exec --tty "$(cat $TMPDIR/cid)" journalctl --pager-end --no-pager --priority info >$TMPFILE
  echo "$TMPFILE"
}

trap err ERR
trap stop EXIT

podman run --interactive --rm --publish 127.0.0.1::22 --detach --privileged --cidfile $TMPDIR/cid --timeout 600 homelab

while [ ! -f $TMPDIR/cid ]; do
  echo "."
  sleep 1
done

ADDR=$(podman port $(cat $TMPDIR/cid) 22)
PORT="${ADDR#*:}"

while [ -z "$(socat -T2 stdout tcp:127.0.0.1:$PORT,connect-timeout=2,readbytes=1 2>/dev/null)" ]; do
  echo "."
  sleep 1
done

set -x

if [ -f "$TMPDIR/_test.yml" ]; then
  ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$TMPDIR/_test.yml"
fi

if [[ ${1:-} == "--checkmode" ]]; then
  shift

  LIST_TAGS=$(ansible-playbook "$TMPDIR/site.yml" --list-tags)

  ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$TMPDIR/site.yml" --check "$@"

  if [[ $LIST_TAGS == *"_check_stage1"* ]]; then
    ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$TMPDIR/site.yml" --tags _check_stage1 "$@"
    ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$TMPDIR/site.yml" --check "$@"
  fi

  if [[ $LIST_TAGS == *"_check_stage2"* ]]; then
    ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$TMPDIR/site.yml" --tags _check_stage2 "$@"
    ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$TMPDIR/site.yml" --check "$@"
  fi

  if [[ $LIST_TAGS == *"_check_stage3"* ]]; then
    ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$TMPDIR/site.yml" --tags _check_stage3 "$@"
    ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$TMPDIR/site.yml" --check "$@"
  fi

  if [[ $LIST_TAGS == *"_check_stage4"* ]]; then
    ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$TMPDIR/site.yml" --tags _check_stage4 "$@"
    ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$TMPDIR/site.yml" --check "$@"
  fi

fi

ansible-playbook -e docker_test=true -e ansible_ssh_port=$PORT --inventory test/inventory.ini "$TMPDIR/site.yml" "$@"
