#!/usr/bin/env bash
#MISE description="Layer packer/seed_deps.yml on top of the box artifact and publish as box_deps"
#MISE interactive=true
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="jammy"
#USAGE complete "ubuntu" run="printf 'jammy\nnoble\nresolute\n'"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail
umask 002

ubuntu="${usage_ubuntu}"
base="${HOMELAB_CI_DIR}/${ubuntu}"
src="${base}/box"
dst="${base}/box_deps"

if [ ! -d "${src}" ]; then
  echo "Source box artifacts missing at ${src}" >&2
  echo "Run 'mise run packer:build box --ubuntu ${ubuntu}' first." >&2
  exit 1
fi

# Stage a copy of box's artifacts into a sibling tmpdir so the seed runs
# against a fresh tree and publish.py's atomic-rename swaps it over the
# previous good box_deps directory without disturbing box. The copy is
# also what protects the box source from --commit's in-place writes,
# since the qcow2 overlay is bypassed.
#
# Use reflink/clone semantics so the copy is near-instant and the
# physical divergence is just the seeded writes (~few hundred MB),
# not the full 40 GB per OS disk. Linux GNU cp's `--reflink=auto`
# triggers the kernel's copy_file_range CoW path (no-op falls back
# to a real read+write copy on filesystems without reflink support).
# macOS cp -R can fall back to byte copy depending on flags + trailing
# slash forms; `ditto` is the Apple-blessed equivalent that always
# uses clonefile(2) when both ends are on the same APFS volume
# (10.13+).
tmp=$(mktemp -d "${base}/.seed-XXXXXX")
# rm the tmpdir on any exit path. On success publish.py has already
# renamed it over ${dst}, so `rm -rf` is a no-op. On failure mid-run
# we don't want a partially-seeded directory left behind to be picked
# up by a future packer publish or confuse the next seed-deps run.
trap 'rm -rf "${tmp}"' EXIT
echo "==> Staging ${src} -> ${tmp}"
case "$(uname -s)" in
Linux) cp -R --reflink=auto "${src}/." "${tmp}/" ;;
Darwin) ditto "${src}" "${tmp}" ;;
*)
  echo "Unsupported OS: $(uname -s)" >&2
  exit 1
  ;;
esac

# launch.py --commit mounts ${tmp}/packer-ubuntu-N.<format> directly
# (no qcow2 overlay), so seed_deps.yml's writes land in the staged tree.
# --seed runs the playbook after system-running, then powers off cleanly.
echo "==> Seeding via test/launch.py --commit"
test/launch.py \
  --machine box_deps \
  --ubuntu "${ubuntu}" \
  --image-dir "${tmp}" \
  --seed packer/seed_deps.yml \
  --commit \
  --timeout 1200

# Atomic publish under the same lockfile packer's install post-processor
# and test/machine.py's shared-flock acquire use. rm + mv windows
# concurrent with a test cell mid-launch would otherwise race the
# backing-file open(2) qemu does at device init.
echo "==> Publishing ${tmp} -> ${dst}"
python3 packer/publish.py \
  "${base}/.publish-lock" \
  "${tmp}" \
  "${dst}"

echo "==> box_deps published at ${dst}"
