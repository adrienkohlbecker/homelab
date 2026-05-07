# ZFSBootMenu aarch64 kexec panic — minimal reproducer

Self-contained script that reproduces the ZBM `kexec`-on-aarch64 panic
described in `../zbm-aarch64-kexec-bug-report.md`.

## What it does

1. Clones zfsbootmenu at the requested git ref (default `v3.1.0`).
2. `sed`-patches upstream's `releng/docker/Dockerfile` to override Void's
   default mirror config so the `XBPS_REPOS` build-arg actually wins (xbps
   merges configs from `/etc/xbps.d/` + `/usr/share/xbps.d/` with
   same-name files in `/etc/` overriding `/usr/share/`).
3. Builds the container with `docker buildx` for the host arch (no
   cross-build).
4. Copies upstream's `release.yaml`-equivalent config + dracut overlays,
   overrides to **Components mode** (no UKI, no systemd-boot stub) plus an
   arch-appropriate `earlycon` so kernel printk reaches serial regardless
   of console subsystem state.
5. Runs the container to produce `vmlin*-bootmenu` + `initramfs-bootmenu.img`.
6. Creates a COW overlay of the user-supplied base qcow2.
7. **Control test:** extracts the on-pool kernel + initrd from the qcow2
   by spinning up a one-shot Ubuntu cloud-image VM (apt-installs
   `zfsutils-linux`, mounts a 9p host share, imports the rpool, copies
   files, poweroffs). Then boots the extracted kernel + initrd directly
   via `qemu-system-* -kernel ... -initrd ...` with an auto-poweroff
   init. A successful run prints `CONTROL-BOOT-OK` and qemu exits cleanly,
   confirming the on-pool kernel itself is bootable on this firmware +
   qemu combo (any failure here would invalidate the kexec reproducer
   below). No host kernel ZFS support required — qemu does the work.
   First run downloads ~600 MB cloud image and apt-installs ZFS (~3-5 min);
   re-runs are faster if `WORKDIR=` is pinned so the cached image is reused.
8. Boots qemu with `-kernel`/`-initrd` against ZBM's kernel + initrd, qemu
   virt machine, EDK2 firmware. ZBM's bash UI imports the rpool and
   `kexec`s the on-pool kernel — and *this* is where the panic happens
   on aarch64.

## Usage

```
./run.sh /path/to/zfs-rooted-ubuntu.qcow2 [v3.1.0|master]
```

The base qcow2 must be a ZFS-rooted Linux install with:
- a ZFS pool whose default boot dataset is `<pool>/ROOT/<release>`,
- a kernel + initramfs installed under `<pool>/ROOT/<release>/boot/`.

### Where to get a base qcow2

There's no widely-published "ZFS-rooted Ubuntu qcow2" download. Three good
sources, ranked by ease for upstream maintainers:

1. **Upstream ZBM's own test harness** — `src/testing/setup.sh -a` builds
   a 5 GB raw image with a Void minimal install on a `ztest` pool, exactly
   the canonical ZBM testbed. (Convert to qcow2 with `qemu-img convert`
   after.) Requires host ZFS userspace + loopback + kvm — works on Linux
   with ZFS, not on macOS/Docker Desktop.
2. **Ubuntu Server installer's "ZFS on Root" option** — produces a
   ZFS-rooted Ubuntu install. Slow but turn-key.
3. **Build via packer / your own automation** — what the homelab repo
   that produced this reproducer does.

The `org.zfsbootmenu:commandline` ZFS property on `rpool/ROOT` should be
set so the kernel cmdline in kexec includes `earlycon=` and `console=` —
otherwise you'll see the panic *or* a silent boot post-kexec depending on
state. To inspect/set after booting the base image once normally:
```
zfs get org.zfsbootmenu:commandline rpool/ROOT
zfs set org.zfsbootmenu:commandline="earlycon=pl011,0x9000000,115200 console=ttyAMA0,115200" rpool/ROOT
```

## Expected output

**On aarch64** (Mac via HVF, or arm64 Linux via KVM): ZBM's bash UI runs,
imports the rpool, auto-boots the highest on-pool kernel via kexec, and the
new kernel panics in `early_pgtable_alloc` with `[Firmware Bug]: Kernel
image misaligned at boot, please fix your bootloader!`.

**On x86_64** (Linux via KVM, or Intel Mac via HVF): the same flow runs to
completion — ZBM kexecs the on-pool kernel and reaches userspace.

The same `vmlin*-bootmenu` + `initramfs-bootmenu.img` boots fine on aarch64
when launched without the kexec hop (e.g. via `qemu-system-aarch64 -kernel
... -initrd ...` directly, no qcow2 attached) — only the kexec from running
ZBM into the on-pool kernel fails.

## Prereqs

- `docker` with the `buildx` plugin (`docker buildx version` works)
- `qemu-system-aarch64` *or* `qemu-system-x86_64` matching the host arch
- EDK2 firmware:
  - macOS (Homebrew QEMU): `/opt/homebrew/share/qemu/edk2-{aarch64,x86_64}-code.fd`
  - Debian/Ubuntu: `qemu-efi-aarch64` and/or `ovmf` packages
  - Fedora/RHEL: `edk2-aarch64` and/or `edk2-ovmf` packages
- `git`, `qemu-img`, `bash`
- For the control-test extraction step: an internet connection (to fetch
  the Ubuntu cloud image and apt packages). No special host kernel
  features needed — extraction runs entirely inside a transient qemu VM.

## Cleanup

The script writes everything into a fresh `mktemp -d` workdir and prints
the path on startup. Remove with `rm -rf <workdir>` when done. Override
with `WORKDIR=...` env to choose a stable location for repeated runs (the
git clone and container layers will be reused; docker buildx layer cache
also helps).

```
docker image rm zbm-repro:aarch64 zbm-repro:x86_64   # if rebuilding from scratch
```
