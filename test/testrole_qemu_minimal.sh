#!/usr/bin/env bash
set -euxo pipefail

ROLE=$1
shift

source .venv/bin/activate

IMAGEDIR=/mnt/qemu
SSH_USER=ubuntu
SSH_KEY=packer/vagrant.key
SSH_HOST=127.0.0.1
UBUNTU_NAME=jammy
UBUNTU_VERSION=22.04
ANSIBLE_ARGS="-e {\"qemu_test\":true} -e @host_vars/box-qemu-minimal.yml"
IDFILE=pid

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

cloud-localds $WORKDIR/seed.img test/minimal/user-data test/minimal/meta-data
qemu-img create -f qcow2 -b $IMAGEDIR/ubuntu-$UBUNTU_VERSION-minimal-cloudimg-amd64.img -F qcow2 $WORKDIR/disk.img 20G > /dev/null

timeout --kill-after=10s 10m \
qemu-system-x86_64 \
-drive "file=$WORKDIR/disk.img,if=virtio,cache=unsafe,discard=unmap,format=qcow2,detect-zeroes=unmap" \
-drive "file=$WORKDIR/seed.img,if=virtio,format=raw" \
-netdev "user,id=user.0,hostfwd=tcp:$SSH_HOST:0-:22" \
-object rng-random,id=rng0,filename=/dev/urandom -device virtio-rng-pci,rng=rng0 \
-machine "type=q35,accel=kvm" \
-smp "8,sockets=8" \
-name "packer-ubuntu" \
-m "4096M" \
-cpu "host" \
-display none \
-device "virtio-net,netdev=user.0" \
-pidfile "$WORKDIR/$IDFILE" \
&
TIMEOUT_PID=$!

stop() {
  kill $TIMEOUT_PID || true
  wait $TIMEOUT_PID || true
  rm -rf "$WORKDIR"
}
trap stop EXIT

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

# lsof -i -P | grep 2096850
# qemu-syst 2096850   ak   12u  IPv4 56342520      0t0  TCP *:5900 (LISTEN)
# qemu-syst 2096850   ak   15u  IPv4 56351807      0t0  TCP localhost:45993 (LISTEN)
# qemu-syst 2096850   ak   32u  IPv4 56346212      0t0  UDP *:52324
# qemu-syst 2096850   ak   33u  IPv4 56346213      0t0  UDP *:35411
PORT=$(lsof -i -P | grep "$(cat $WORKDIR/$IDFILE)" | grep TCP | grep -v "*:59" | cut -d':' -f2 | cut -d' ' -f1)

while [ -z "$(socat -T2 stdout tcp:127.0.0.1:$PORT,connect-timeout=2,readbytes=1 2>/dev/null)" ]; do
  echo -n "."
  sleep 1
done
echo "SSH up"

SSH_CMD="ssh -i $SSH_KEY -p $PORT -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $SSH_USER@$SSH_HOST"
stop() {
  if [ "${KEEPAROUND:-}" = "1" ]; then
    echo "Keeping VM around, ssh using:"
    echo "> $SSH_CMD"
    echo "Then Ctrl+C or"
    echo "> kill $TIMEOUT_PID"
    trap 'kill $TIMEOUT_PID' INT
  else
    kill $TIMEOUT_PID || true
  fi
  wait $TIMEOUT_PID || true
  rm -rf "$WORKDIR"
}
trap stop EXIT

err() {
  TMPFILE=test/out/$ROLE.journal.ansi
  $SSH_CMD env SYSTEMD_COLORS=true journalctl --pager-end --no-pager --priority info >$TMPFILE
  echo "$TMPFILE"
}
trap err ERR

$SSH_CMD sudo bash <<EOF
truncate -s0 /etc/apt/sources.list
echo "deb http://apt.lab.fahm.fr/archive.ubuntu.com/ubuntu/ $UBUNTU_NAME main restricted universe multiverse" >> /etc/apt/sources.list
echo "deb http://apt.lab.fahm.fr/archive.ubuntu.com/ubuntu/ $UBUNTU_NAME-updates main restricted universe multiverse" >> /etc/apt/sources.list
echo "deb http://apt.lab.fahm.fr/archive.ubuntu.com/ubuntu/ $UBUNTU_NAME-security main restricted universe multiverse" >> /etc/apt/sources.list
echo "deb http://apt.lab.fahm.fr/archive.ubuntu.com/ubuntu/ $UBUNTU_NAME-backports main restricted universe multiverse" >> /etc/apt/sources.list
apt-get update
EOF
$SSH_CMD sudo apt-get purge --autoremove --yes snapd

export ANSIBLE_DISPLAY_OK_HOSTS=true
export ANSIBLE_DISPLAY_SKIPPED_HOSTS=true

ANSIBLE_PLAYBOOK="ansible-playbook $ANSIBLE_ARGS -e ansible_ssh_port=$PORT -e ansible_ssh_host=$SSH_HOST -e ansible_ssh_user=$SSH_USER -e ansible_ssh_private_key_file=$SSH_KEY -e ubuntu_mirror=http://apt.lab.fahm.fr/archive.ubuntu.com/ubuntu/ -e ubuntu_mirror_security=http://apt.lab.fahm.fr/security.ubuntu.com/ubuntu/ --inventory test/inventory.ini"

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
