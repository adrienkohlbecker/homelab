#!/usr/bin/env bash
# GitLab Fleeting's instance_ready_command: the host takes jobs only once
# scratch is mounted and the KVM, runner, qemu, and mise toolchain are usable.
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
command -v "qemu-system-$(uname -m)" >/dev/null
command -v qemu-img >/dev/null
command -v passt >/dev/null
command -v mise >/dev/null
