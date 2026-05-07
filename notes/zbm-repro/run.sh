#!/usr/bin/env bash
# Minimal reproducer for ZFSBootMenu / aarch64 / EDK2 / QEMU virt kexec
# alignment panic. Builds ZBM in Components mode (kernel + initrd, no UKI),
# then boots ZBM via QEMU's -kernel/-initrd against a user-provided
# ZFS-rooted Ubuntu qcow2. ZBM's `kexec` to the on-pool kernel reproduces
# the "Kernel image misaligned at boot" panic on aarch64.
#
# Same script can be run on x86_64 for the control case (we expect the
# kexec hop to succeed there).
#
# Usage: ./run.sh <path-to-zfs-rooted-ubuntu-qcow2> [zbm-git-ref]
#   zbm-git-ref defaults to v3.1.0
#
# Prereqs:
#   - docker with the buildx plugin (`docker buildx version` works)
#   - qemu-system-aarch64 or qemu-system-x86_64 with EDK2 firmware (Mac:
#     Homebrew `qemu` formula, Linux: ovmf / qemu-efi-aarch64 packages)
#   - git, qemu-img
#
# The provided base image must be an Ubuntu install with a ZFS rpool whose
# default dataset is rpool/ROOT/<release>, an EFI partition that mounts to
# /boot/efi, and a kernel installed under rpool/ROOT/<release>/boot/. The
# specifics don't matter for the bug — ZBM's bash UI imports the pool, picks
# the highest-versioned kernel, and `kexec`s to it. The panic is in that
# kexec step.

set -euxo pipefail

BASE_IMAGE="${1:?usage: $0 <base-image-qcow2> [zbm-ref]}"
ZBM_REF="${2:-v3.1.0}"

if [ ! -f "$BASE_IMAGE" ]; then
  echo "base image not found: $BASE_IMAGE" >&2
  exit 1
fi
BASE_IMAGE="$(cd "$(dirname "$BASE_IMAGE")" && pwd)/$(basename "$BASE_IMAGE")"

WORKDIR="${WORKDIR:-$(mktemp -d -t zbm-repro.XXXXXX)}"
echo "Working in: $WORKDIR"
cd "$WORKDIR"

case "$(uname -m)" in
  arm64|aarch64) ARCH=aarch64 ;;
  x86_64|amd64)  ARCH=x86_64 ;;
  *) echo "unsupported host arch: $(uname -m)" >&2; exit 1 ;;
esac
echo "Building for arch: $ARCH"

# Resolve qemu binary, accelerator, machine type, EFI firmware path, and
# arch-specific kernel cmdline early so both the control test and the ZBM
# kexec test below can use the same values.
case "$ARCH" in
  aarch64) EARLYCON="earlycon=pl011,0x9000000,115200 console=ttyAMA0,115200" ;;
  x86_64)  EARLYCON="earlycon=uart8250,io,0x3f8 console=ttyS0,115200" ;;
esac

case "$(uname)-$ARCH" in
  Darwin-aarch64)
    QEMU_BIN=qemu-system-aarch64
    ACCEL=hvf
    MACHINE=virt
    EFI_CODE=/opt/homebrew/share/qemu/edk2-aarch64-code.fd
    EXTRA_DEVS="-device virtio-gpu-pci -device qemu-xhci -device usb-kbd"
    ;;
  Linux-aarch64)
    QEMU_BIN=qemu-system-aarch64
    ACCEL=kvm
    MACHINE=virt
    EFI_CODE=/usr/share/AAVMF/AAVMF_CODE.fd
    EXTRA_DEVS="-device virtio-gpu-pci -device qemu-xhci -device usb-kbd"
    ;;
  Darwin-x86_64)
    QEMU_BIN=qemu-system-x86_64
    ACCEL=hvf
    MACHINE=q35
    EFI_CODE=/opt/homebrew/share/qemu/edk2-x86_64-code.fd
    EXTRA_DEVS=""
    ;;
  Linux-x86_64)
    QEMU_BIN=qemu-system-x86_64
    ACCEL=kvm
    MACHINE=q35
    EFI_CODE=/usr/share/OVMF/OVMF_CODE.fd
    EXTRA_DEVS=""
    ;;
esac

if [ ! -f "$EFI_CODE" ]; then
  echo "EFI firmware not found at $EFI_CODE — install your distro's ovmf / qemu-efi-aarch64 package" >&2
  exit 1
fi

#######################################################################
# 1. Clone zfsbootmenu at the requested ref.
#######################################################################
if [ ! -d src ]; then
  git clone --depth 1 --branch "$ZBM_REF" \
    https://github.com/zbm-dev/zfsbootmenu.git src
fi

#######################################################################
# 2. Patch upstream Dockerfile: rename the xbps repo conf the upstream
#    Dockerfile writes to so it overrides Void's default
#    /usr/share/xbps.d/00-repository-main.conf instead of just adding
#    a parallel /etc/xbps.d/00-custom-repos.conf. xbps merges all repo
#    configs from /etc/xbps.d/ + /usr/share/xbps.d/, with same-name files
#    in /etc/ overriding /usr/share/. Without this, XBPS_REPOS adds a
#    second repo alongside Void's default, and xbps may still query the
#    slow upstream mirror.
#
#    No other patching needed — buildx supports the BuildKit heredoc and
#    `--mount=type=cache` syntax that upstream's Dockerfile uses.
#######################################################################
sed -i.bak 's|/etc/xbps.d/00-custom-repos.conf|/etc/xbps.d/00-repository-main.conf|' \
  src/releng/docker/Dockerfile

#######################################################################
# 3. Build the container. Void's mirror layout has glibc-x86_64 at
#    /current and aarch64 at /current/aarch64; pick the right subpath
#    for the host arch.
#######################################################################
case "$ARCH" in
  x86_64)  XBPS_REPO="https://repo-de.voidlinux.org/current" ;;
  aarch64) XBPS_REPO="https://repo-de.voidlinux.org/current/aarch64" ;;
esac

docker buildx build \
  --build-arg "XBPS_REPOS=$XBPS_REPO" \
  --tag "zbm-repro:$ARCH" \
  --load \
  -f src/releng/docker/Dockerfile \
  src/releng/docker

#######################################################################
# 4. Compose ZBM build inputs:
#    - config.yaml: derived from upstream release.yaml; overridden to
#      Components mode (no UKI; we use rEFInd-style discrete kernel +
#      initrd output). Embeds an arch-appropriate earlycon + console for
#      the kexec-target kernel cmdline (set via ZFS property org.zfsbootmenu:commandline
#      on the rpool's ROOT dataset; not done by this script — assumed in the
#      base image, or override at the menu).
#    - dracut.conf.d/common.conf + recovery.conf: copied verbatim from
#      upstream so the resulting initramfs has the standard ZBM rescue
#      tools (gdisk, parted, ssh, kernel-network-modules, etc.).
#######################################################################
mkdir -p build/dracut.conf.d build/output

# Start from upstream's release.yaml verbatim, then patch two fields:
# (a) flip EFI.Enabled to false to switch from UKI to Components output;
# (b) override Kernel.CommandLine with our arch-specific earlycon + a
#     verbose loglevel so kernel printk reaches serial reliably.
# The EFI.Enabled patch is range-addressed (EFI: → Kernel:) to avoid
# touching Components.Enabled which is also "Enabled: true".
cp src/etc/zfsbootmenu/release.yaml build/config.yaml
sed -i.bak '/^EFI:/,/^Kernel:/ s|^  Enabled: true$|  Enabled: false|' build/config.yaml
sed -i.bak "s|^  CommandLine: .*|  CommandLine: $EARLYCON loglevel=7|" build/config.yaml
rm -f build/config.yaml.bak

# cp -L follows symlinks (recovery.conf.d/common.conf points at ../release.conf.d/common.conf).
cp -L src/etc/zfsbootmenu/release.conf.d/common.conf   build/dracut.conf.d/common.conf
cp -L src/etc/zfsbootmenu/recovery.conf.d/recovery.conf build/dracut.conf.d/recovery.conf

#######################################################################
# 5. Build the ZBM kernel + initrd.
#    The container's build-init.sh fetches zfsbootmenu source (-t REF),
#    runs generate-zbm against /build/config.yaml, and emits its output
#    under /output. With Components mode, we get vmlinuz-bootmenu (or
#    vmlinux-bootmenu on aarch64, since the kernel is uncompressed there)
#    plus initramfs-bootmenu.img.
#######################################################################
docker run --rm \
  -v "$WORKDIR/build:/build:ro" \
  -v "$WORKDIR/build/output:/output" \
  "zbm-repro:$ARCH" \
  -o /output -t "$ZBM_REF"

#######################################################################
# 6. COW overlay the user-provided base image so each run is fresh.
#######################################################################
qemu-img create -f qcow2 -b "$BASE_IMAGE" -F qcow2 disk.qcow2

# Empty NVRAM — we boot via -kernel, so the firmware doesn't need a
# bootable EFI binary on the ESP.
truncate -s 64M efivars.fd

#######################################################################
# 7. Extract the on-pool kernel + initrd from the qcow2 and boot them
#    DIRECTLY (no ZBM, no kexec) as a control test. If this boots cleanly
#    to userspace, the kernel binary on the rpool is fine — which is the
#    crucial baseline for the ZBM kexec reproducer that follows.
#
#    Strategy: spin up a one-shot Ubuntu cloud-image VM with cloud-init,
#    apt-install zfsutils-linux inside it, attach the user's qcow2 as a
#    second disk, mount a 9p share to a host directory, copy the on-pool
#    kernel + initrd to that share, then poweroff. Files appear directly
#    on the host. Works wherever qemu does — no host kernel ZFS support
#    needed (which Mac/Docker Desktop / podman-machine don't have).
#
#    First run downloads the ~600 MB cloud image and apt-installs ZFS
#    (~3-5 min). The cloud image is cached in $WORKDIR (use a stable
#    WORKDIR for repeated runs to keep it).
#######################################################################
echo
echo "=== extracting kernel + initrd from qcow2 (via Ubuntu cloud image) ==="
mkdir -p extracted

UBUNTU_RELEASE="${UBUNTU_RELEASE:-jammy}"
case "$ARCH" in
  aarch64) CLOUD_ARCH=arm64 ;;
  x86_64)  CLOUD_ARCH=amd64 ;;
esac

if [ ! -f /tmp/cloud-base.qcow2 ]; then
  echo "Downloading Ubuntu $UBUNTU_RELEASE cloud image (~600 MB, one time)..."
  curl -fL --progress-bar -o cloud-base.qcow2.tmp \
    "https://cloud-images.ubuntu.com/${UBUNTU_RELEASE}/current/${UBUNTU_RELEASE}-server-cloudimg-${CLOUD_ARCH}.img"
  mv cloud-base.qcow2.tmp /tmp/cloud-base.qcow2
fi

# COW overlay + grow to leave room for apt install (cloud image is ~3 GB).
qemu-img create -f qcow2 -b /tmp/cloud-base.qcow2 -F qcow2 cloud-extract.qcow2
qemu-img resize cloud-extract.qcow2 8G

# Cloud-init NoCloud seed: install zfsutils, mount a 9p share to /share,
# import rpool, copy kernel + initrd to /share, then poweroff.
mkdir -p seed
cat > seed/user-data <<'EOF'
#cloud-config
package_update: true
packages:
  - zfsutils-linux
runcmd:
  - |
    set -ex
    mkdir -p /share
    modprobe 9pnet_virtio || true
    mount -t 9p -o trans=virtio,version=9p2000.L share /share
    zpool import -fN -R /mnt rpool
    active=$(zfs list -H -o name -t filesystem | grep -m1 'rpool/ROOT/')
    zfs mount "$active"
    # ZFS mountpoint = dataset's mountpoint property + altroot. Standard
    # rpool/ROOT/<release> has mountpoint=/, altroot=/mnt, so the dataset
    # ends up at /mnt — *not* /mnt/ROOT/<release>/. findmnt discovers the
    # actual path so this works for non-standard layouts too.
    mp=$(findmnt -nro TARGET --source "$active")
    # Pick the highest-versioned kernel/initrd. The `vmlinu[xz]-*` glob
    # excludes the unversioned `vmlinuz` / `vmlinuz.old` symlinks Ubuntu
    # ships, which would otherwise trip cp ("target is not a directory").
    kernel=$(ls "$mp/boot/"vmlinuz-* "$mp/boot/"vmlinux-* 2>/dev/null | sort -V | tail -1)
    initrd=$(ls "$mp/boot/"initrd.img-* 2>/dev/null | sort -V | tail -1)
    cp -L "$kernel" /share/kernel
    cp -L "$initrd" /share/initrd
    sync
power_state:
  mode: poweroff
  delay: now
  message: "extraction-complete"
EOF
cat > seed/meta-data <<EOF
instance-id: zbm-extractor-$(date +%s)
local-hostname: zbm-extractor
EOF

# Build the seed.iso in a container so we don't depend on host
# genisoimage/cloud-image-utils being installed.
docker run --rm \
  -v "$WORKDIR/seed:/in:ro" \
  -v "$WORKDIR:/out" \
  ubuntu:22.04 bash -euc '
    apt-get update -qq
    apt-get install -qq -y cloud-image-utils
    cloud-localds /out/seed.iso /in/user-data /in/meta-data
  '

# Empty NVRAM for the extractor VM.
truncate -s 64M cloud-efivars.fd

# Boot the cloud-image VM. cloud-init runs the runcmd then poweroffs;
# qemu exits cleanly. The extracted files appear in $WORKDIR/extracted/.
# `mapped-xattr` 9p model stores ownership in xattrs, avoiding chown
# failures when root-in-guest writes to a Mac-userland-owned host dir.
# shellcheck disable=SC2086
"$QEMU_BIN" \
  -accel "$ACCEL" -machine "$MACHINE" -cpu host \
  -smp 4 -m 4096 \
  -drive file="$EFI_CODE",if=pflash,unit=0,format=raw,readonly=on \
  -drive file=cloud-efivars.fd,if=pflash,unit=1,format=raw \
  -drive file=cloud-extract.qcow2,if=virtio,format=qcow2 \
  -drive file=seed.iso,if=virtio,format=raw,readonly=on \
  -drive file=disk.qcow2,if=virtio,format=qcow2 \
  -fsdev local,id=share,path="$WORKDIR/extracted",security_model=mapped-xattr \
  -device virtio-9p-pci,fsdev=share,mount_tag=share \
  -netdev user,id=u0 -device virtio-net,netdev=u0 \
  $EXTRA_DEVS \
  -nographic | tee /tmp/extractor.log

if [ ! -f extracted/kernel ] || [ ! -f extracted/initrd ]; then
  echo "*** extraction failed — extracted/kernel and/or extracted/initrd not produced ***" >&2
  echo "Check the cloud-image VM's serial output above for the cloud-init runcmd error." >&2
  exit 1
fi

EXTRACTED_KERNEL="$WORKDIR/extracted/kernel"
EXTRACTED_INITRD="$WORKDIR/extracted/initrd"
echo "extracted: $EXTRACTED_KERNEL"
echo "extracted: $EXTRACTED_INITRD"

# ----- control test: boot the extracted kernel directly. Linux's kernel
# cmdline parser doesn't honor shell quotes, so we can't pass an inline
# `init=/bin/sh -c 'echo SUCCESS; poweroff'`-style auto-shutdown — the
# quoted string gets word-split before sh ever sees it. Instead, just
# `timeout` the qemu process and check the serial log for boot-success
# markers (login prompt / systemd "Reached target" / Cloud-init finished)
# vs. the failure markers we'd see if the kernel itself was broken.
echo
echo "=== control test: boot on-pool kernel directly via -kernel ==="
qemu-img create -f qcow2 -b "$BASE_IMAGE" -F qcow2 control-disk.qcow2
truncate -s 64M control-efivars.fd

CONTROL_APPEND="$EARLYCON loglevel=7 root=ZFS=rpool/ROOT/jammy ro"

# Piping through `tee` block-buffers on a non-TTY sink, so output can
# vanish when `timeout` SIGTERMs qemu before any flush. Send qemu's
# serial straight to a file via `-serial file:` so kernel printk lands
# on disk a line at a time, regardless of buffering. Live-tail in the
# background so the user still sees boot progress.
: > control.log
tail -f control.log &
TAIL_PID=$!

set +e
# shellcheck disable=SC2086
timeout 30 "$QEMU_BIN" \
  -accel "$ACCEL" -machine "$MACHINE" -cpu host \
  -smp 4 -m 4096 \
  -kernel "$EXTRACTED_KERNEL" \
  -initrd "$EXTRACTED_INITRD" \
  -append "$CONTROL_APPEND" \
  -drive file="$EFI_CODE",if=pflash,unit=0,format=raw,readonly=on \
  -drive file=control-efivars.fd,if=pflash,unit=1,format=raw \
  -drive file=control-disk.qcow2,if=virtio,format=qcow2 \
  -netdev user,id=u0 -device virtio-net,netdev=u0 \
  $EXTRA_DEVS \
  -display none -monitor none -serial file:control.log
control_rc=$?
set -e

kill "$TAIL_PID" 2>/dev/null || true
sleep 0.2  # let any final lines flush

if grep -qE 'login:|Reached target.*Multi-User|cloud-init.*finished' control.log; then
  echo "*** control test PASSED — on-pool kernel reached userspace via -kernel ***"
elif grep -qE 'Kernel image misaligned|Kernel panic - not syncing' control.log; then
  echo "*** control test FAILED with the *same* misalignment/panic — bug isn't ZBM-specific!" >&2
  echo "    See $WORKDIR/control.log" >&2
else
  echo "*** control test inconclusive (timeout=30s, rc=$control_rc); see $WORKDIR/control.log ***" >&2
fi

#######################################################################
# 8. Boot ZBM via -kernel/-initrd, with the qcow2 attached so ZBM can
#    import the rpool and find the on-pool kernel for kexec. The kernel
#    cmdline includes earlycon so kernel printk goes to serial regardless
#    of console subsystem state — we want to see the panic if it happens.
#######################################################################
KERNEL=$(ls build/output/vmlin*-bootmenu)
INITRD=$(ls build/output/initramfs-bootmenu.img)

cat <<INFO

=== reproducer ready ===
WORKDIR : $WORKDIR
ARCH    : $ARCH
ZBM ref : $ZBM_REF
kernel  : $KERNEL
initrd  : $INITRD
disk    : $WORKDIR/disk.qcow2 (COW overlay of $BASE_IMAGE)

Booting ZBM. Expected on aarch64: ZBM bash UI appears, imports rpool,
auto-boots the highest on-pool kernel via kexec, and panics with
"Kernel image misaligned at boot" + "Failed to allocate page table page"
on early_pgtable_alloc. Press Ctrl-A X to quit qemu when you've seen
enough.

INFO

# $EXTRA_DEVS deliberately word-splits into multiple qemu args; quoting
# it would pass it as a single arg with embedded spaces, which qemu rejects.
# shellcheck disable=SC2086
exec "$QEMU_BIN" \
  -accel "$ACCEL" -machine "$MACHINE" -cpu host \
  -smp 4 -m 8192 \
  -kernel "$KERNEL" \
  -initrd "$INITRD" \
  -append "$EARLYCON loglevel=7 zbm.show" \
  -drive file="$EFI_CODE",if=pflash,unit=0,format=raw,readonly=on \
  -drive file=efivars.fd,if=pflash,unit=1,format=raw \
  -drive file=disk.qcow2,if=virtio,format=qcow2 \
  -netdev user,id=u0 -device virtio-net,netdev=u0 \
  $EXTRA_DEVS \
  -nographic
