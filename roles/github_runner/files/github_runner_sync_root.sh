#!/bin/bash
# Sync the canonical actions-runner tree into a per-instance dir so each
# github_runner@<instance>.service has its own RUNNER_ROOT.
#
# Why per-instance: actions/runner writes .runner / .credentials /
# .credentials_rsaparams / _diag/ into the dir containing its binaries.
# With N instances sharing /opt/actions-runner :rw, their startups race
# the same files; whichever container's startup loses the last-write
# race signs session messages with another instance's RSA key, and the
# GitHub message broker rejects with "The signature is not valid". The
# error is classified retryable by actions/runner, so the listener
# in-process re-launches every ~30s without ever exiting -- the
# pgrep-based healthcheck stays green, systemd never restarts the
# container, and the runner shows up as offline in the GitHub UI for
# the entire lifetime of the (stuck) container. Giving each instance
# its own RUNNER_ROOT breaks the race at the source.
#
# Why hard links over copies: the actions-runner install is ~500MB.
# Copying for N instances multiplies that; hard links share the inode
# across paths so the binary tree stays at 1x on disk. Refreshing on
# a tarball bump is just rm + re-link; both the canonical extract and
# this sync are cheap.
#
# Why hard links over symlinks: the unit shadow-binds this per-instance
# dir at /opt/actions-runner inside the container. A symlink at
# /opt/actions-runner/<inst>/bin pointing to "/opt/actions-runner/bin"
# (absolute) would self-reference inside the container -- the target
# path resolves through the bind-mount back into this per-instance dir,
# hitting the same symlink and producing ELOOP. Relative symlinks
# (../bin) would land outside the bind mount entirely, which podman
# doesn't expose to the container. Hard links sidestep both by being
# directory entries on the same inode; no path resolution involved at
# read time. The disk-efficiency story (inode sharing keeps the 500MB
# tree at 1x across N instances vs N copies) holds independently.
#
# Invoked from the unit template as ExecStartPre, so it runs as root
# before every container start. Idempotent: on an unchanged tarball,
# the rm + cp -al rewrites identical hard links pointing at the same
# inodes (~ms). On a tarball bump (github_runner_extract.changed
# triggers a restart via _register.yml's systemd_unit restart chain),
# the rm side drops the now-stale hard links and the cp -al side
# re-creates them pointing at the new inodes.
set -euo pipefail

inst="${1:?usage: $0 <instance>}"

src=/opt/actions-runner
dst="$src/$inst"

[[ -d "$src" ]] || { echo >&2 "$0: canonical $src missing"; exit 1; }

install -d -o github_runner -g github_runner -m 0755 "$dst"

# Source-of-truth list. Only tarball-shipped binaries / scripts /
# templates get hard-linked; runtime state files (.runner /
# .credentials* / _diag / run-helper.sh) stay per-instance and are
# never touched here. .runner.tar.gz lives only at the canonical root.
# Iterating an explicit list (instead of `cp -al $src/*`) keeps state
# files at the canonical root from accidentally getting hard-linked
# into the per-instance dir during the orphan-cleanup window after a
# layout migration -- they'd be ambiguous later about which instance
# owned them.
entries=(
  bin
  externals
  run.sh
  env.sh
  config.sh
  safe_sleep.sh
  run-helper.sh
  run-helper.sh.template
  run-helper.cmd.template
)

for entry in "${entries[@]}"; do
  src_path="$src/$entry"
  dst_path="$dst/$entry"
  [[ -e "$src_path" ]] || continue
  # rm -rf the dst entry first so a tarball-bump-changed inode gets
  # re-linked rather than the cp -al silently merging into a stale
  # directory tree. cp -al's default behavior on an existing target
  # directory is "copy into" which would double-nest bin/bin/...
  rm -rf "$dst_path"
  cp -al "$src_path" "$dst_path"
done
