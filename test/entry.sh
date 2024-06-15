#!/usr/bin/env bash
set -euo pipefail

if [ "$(basename "$1")" != "sshd" ]; then
  exec "$@"
fi

has_keys=false
for file in /etc/ssh/ssh_host_*; do
  if [ -f "$file" ]; then
    has_keys=true
    break
  fi
done

# Generate Host keys, if required
if [ $has_keys = false ]; then
  ssh-keygen -A
fi

# refresh apt cache
# apt-get update >/dev/null

stop() {
  echo "Received SIGINT or SIGTERM. Shutting down sshd"
  pid=$(cat /var/run/sshd/sshd.pid)
  kill -SIGTERM "$pid"
  wait "$pid"
}

trap stop SIGINT SIGTERM
"$@" &
pid="$!"
mkdir -p /var/run/sshd && echo "$pid" >/var/run/sshd/sshd.pid
wait "$pid"
exit $?
