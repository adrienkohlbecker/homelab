#!/usr/bin/env bash
set -euo pipefail

ROLE=$1
shift

TMPDIR=$(mktemp -d)
trap 'rm -rf $TMPDIR' EXIT

cp -r group_vars "$TMPDIR"
cp -r host_vars "$TMPDIR"
cp -r "roles/systemd_unit" "$TMPDIR"
cp -r "roles/usergroup_immediate" "$TMPDIR"
cp -r "roles/apt_unit_masked" "$TMPDIR"
cp -r "roles/logrotate" "$TMPDIR"
cp -r "roles/nginx" "$TMPDIR"
cp -r "roles/cron" "$TMPDIR"
cp -r "roles/zfs_mount" "$TMPDIR"
cp -r "roles/sort_ini" "$TMPDIR"
cp -r "roles/$ROLE" "$TMPDIR"

cat <<EOF >"$TMPDIR/site.yml"
- hosts: box
  roles:
    - $ROLE
EOF

stop() {
  pid=$(cat "$TMPDIR/docker.pid")
  kill "$pid"
  wait "$pid"

  rm -rf "$TMPDIR"
}

trap stop EXIT

docker run -i --rm -p 2222:22 homelab &
pid="$!"
echo "$pid" >"$TMPDIR/docker.pid"

while [ -z "$(socat -T2 stdout tcp:127.0.0.1:2222,connect-timeout=2,readbytes=1 2>/dev/null)" ]; do
  echo "."
  sleep 1
done

LIST_TAGS=$(ansible-playbook "$TMPDIR/site.yml" --list-tags)

ansible-playbook -e docker_test=true --inventory test/inventory.ini "$TMPDIR/site.yml" --check "$@"

if [[ $LIST_TAGS == *"_check_stage1"* ]]; then
  ansible-playbook -e docker_test=true --inventory test/inventory.ini "$TMPDIR/site.yml" --tags _check_stage1 "$@"
  ansible-playbook -e docker_test=true --inventory test/inventory.ini "$TMPDIR/site.yml" --check "$@"
fi

if [[ $LIST_TAGS == *"_check_stage2"* ]]; then
  ansible-playbook -e docker_test=true --inventory test/inventory.ini "$TMPDIR/site.yml" --tags _check_stage2 "$@"
  ansible-playbook -e docker_test=true --inventory test/inventory.ini "$TMPDIR/site.yml" --check "$@"
fi

ansible-playbook -e docker_test=true --inventory test/inventory.ini "$TMPDIR/site.yml" "$@"
