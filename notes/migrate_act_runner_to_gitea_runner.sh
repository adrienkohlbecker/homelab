#!/usr/bin/env bash
# Run once on lab AFTER applying the rename commit and BEFORE re-running ansible.
#
# The rename in ansible (act_runner -> gitea_runner) plus the upstream binary
# rename in v1.0.0 (act_runner -> gitea-runner) leaves the host with stale
# state under the old names. The new role creates fresh state under the new
# names and doesn't touch the old; this script cleans up.
#
# Side effects:
#   - stops + disables the old act_runner.service
#   - removes the old act_runner system user (userdel; leaves home untouched
#     before we explicitly rm it)
#   - drops the linger marker (loginctl disable-linger is best-effort)
#   - removes the old binary at /usr/local/bin/act_runner
#   - removes the old unit file at /etc/systemd/system/act_runner.service
#   - removes the old config/scratch trees /mnt/services/act_runner and
#     /mnt/scratch/act_runner (each owned by the about-to-be-removed uid)
#   - daemon-reload so systemd forgets the unit
#
# What this script does NOT do:
#   - delete the old runner row in gitea's admin UI. Gitea still has an
#     action_runner row whose name is the VM's prior OS hostname (or "lab"
#     if it was registered with --name lab). Visit
#     https://gitea.lab.fahm.fr/-/admin/runners and delete the offline row
#     after applying ansible has created the new one.
#   - touch /etc/subuid or /etc/subgid. The subid role will overwrite both
#     files on next apply: the act_runner line drops out, the gitea_runner
#     line is added at the same range index.
#
# Idempotent: every step gates on existence, so a re-run is a no-op.

set -euo pipefail

if [[ "$(id -un)" != "root" && "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "must run as root (use sudo)" >&2
  exit 1
fi

# stop+disable so the unit doesn't try to restart while we rip out its user
if systemctl is-loaded act_runner.service >/dev/null 2>&1 \
  || [[ -f /etc/systemd/system/act_runner.service ]]; then
  systemctl stop act_runner.service 2>/dev/null || true
  systemctl disable act_runner.service 2>/dev/null || true
fi

# disable-linger fails if linger isn't on; ignore. Drops the marker file
# AND tells the user manager to exit at next idle.
if [[ -f /var/lib/systemd/linger/act_runner ]]; then
  loginctl disable-linger act_runner || true
fi

# Stop the user manager so the rootless podman.socket releases /run/user/<uid>
# before we userdel; otherwise userdel can leave the runtime dir littered.
if pgrep -u act_runner >/dev/null 2>&1; then
  loginctl terminate-user act_runner || true
  # short settle to let the user manager actually exit
  for _ in 1 2 3 4 5; do
    pgrep -u act_runner >/dev/null 2>&1 || break
    sleep 1
  done
fi

# Now the user can go. userdel leaves $HOME alone (we explicitly rm below).
if getent passwd act_runner >/dev/null 2>&1; then
  userdel act_runner
fi
if getent group act_runner >/dev/null 2>&1; then
  groupdel act_runner 2>/dev/null || true
fi

rm -f /etc/systemd/system/act_runner.service
rm -f /usr/local/bin/act_runner
# Old scratch + services trees -- owned by the just-removed act_runner uid,
# so contents are now owned by an unknown uid until rm clears them.
rm -rf /mnt/services/act_runner
rm -rf /mnt/scratch/act_runner

systemctl daemon-reload

cat <<'EOF'

Old act_runner state cleared.

Next steps:
  1. Run `ansible-playbook site.yml --limit lab --tags gitea_runner` (via
     `mise run ansible`) to create the new gitea_runner user, install the
     v1.0.3 binary, write subuid/subgid entries via the subid role, and
     register a fresh runner against gitea.
  2. Visit https://gitea.lab.fahm.fr/-/admin/runners and delete the old
     offline runner row -- the new gitea_runner one will appear there
     after the apply.

EOF
