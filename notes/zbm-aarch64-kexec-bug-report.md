# ZFSBootMenu v3.1.0 / aarch64 — kexec to on-pool kernel panics with "Kernel image misaligned at boot"

## Summary

On an aarch64 QEMU virt VM with EDK2 firmware, ZFSBootMenu v3.1.0 (built from
upstream `releng/docker/Dockerfile` + custom `config.yaml`) successfully starts,
runs its bash UI, imports the rpool, and identifies the on-pool Ubuntu kernel.
But its `kexec` handoff to that kernel reliably panics very early in arch
setup with:

```
[Firmware Bug]: Kernel image misaligned at boot, please fix your bootloader!
Kernel panic - not syncing: Failed to allocate page table page
```

The same Ubuntu kernel + initrd boots fine when launched directly via QEMU's
`-kernel`/`-initrd` (skipping kexec entirely), so the kernel image itself is
fine. The bug is somewhere in the kexec handoff path on aarch64.

## Environment

| Component | Version |
|---|---|
| Host | macOS 15 on Apple Silicon (M-series) |
| QEMU | 9.2.x (Homebrew, `accel=hvf`) |
| Machine | `qemu-system-aarch64 -machine virt -cpu host` |
| Firmware | `edk2-stable202408-prebuilt.qemu.org` (Sep 12 2024 build, bundled with Homebrew QEMU) |
| ZBM | v3.1.0, Components mode (separate kernel + initrd, no UKI) |
| Builder image | `ghcr.io/void-linux/void-glibc-full` rebuilt locally (Void's `current/aarch64`) |
| ZBM kernel inside the bundle | Void's `linux6.18` |
| kexec-tools | tested both Void's package (2.0.32_1) and upstream HEAD (2.0.32.git) |
| Guest | Ubuntu 22.04 jammy, `linux-image-generic` (5.15.0-177) and `linux-generic-hwe-22.04` (6.8.0-111) — both panic identically |
| ZFS | OpenZFS 2.x (whatever Ubuntu jammy ships) |

## What works end-to-end

- ZBM build via the upstream `releng/docker/Dockerfile` on aarch64 (with Void's `current/aarch64` repo subpath, with `--security-opt seccomp=unconfined` on rootless podman during `xbps-install` to allow xattr setting on extracted files).
- The `.linux`, `.initrd`, `.cmdline`, etc. PE sections in the resulting `vmlinux.EFI` UKI inspect cleanly with `llvm-objcopy --dump-section`.
- Booting that *kernel + initrd* directly via `qemu-system-aarch64 -kernel ./vmlinux-bootmenu -initrd ./initramfs-bootmenu.img -append '...'` produces a working ZBM: bash UI, hostid loaded, rpool imported, kernel entry visible, default-boot countdown.

## What doesn't work

- The systemd-boot aarch64 EFI stub embedded in `vmlinux.EFI` (UKI mode) silently fails to invoke the kernel under EDK2 + QEMU virt. rEFInd / firmware loads the `.EFI`, none of the stub's usual messages (`EFI stub: Booting Linux Kernel...`, `EFI_RNG_PROTOCOL unavailable`, `Using DTB from configuration table`, `Exiting boot services...`) appear on serial, and `earlycon=pl011,...` baked into `.cmdline` produces no output. Worked around by switching to Components mode (`EFI.Enabled: false` in config.yaml).
- Once Components mode is on the wire, ZBM's bash runs and `kexec` is ultimately invoked. **This is where the panic happens.**

## Failure mode — verbatim

ZBM's last serial output:
```
Tearing down USB controller 0000:00:02.0...
Booting /boot/vmlinuz-6.8.0-111-generic for rpool/ROOT/jammy ...
```

Then the new kernel produces:
```
[    0.000000] Booting Linux on physical CPU 0x0000000000 [0x610f0000]
[    0.000000] Linux version 6.8.0-111-generic ...
[    0.000000] KASLR enabled
[    0.000000] random: crng init done
[    0.000000] earlycon: pl11 at MMIO 0x0000000009000000 (options '115200')
[    0.000000] printk: legacy bootconsole [pl11] enabled
[    0.000000] efi: Can't find property 'linux,uefi-secure-boot' in DT!
[    0.000000] [Firmware Bug]: Kernel image misaligned at boot, please fix your bootloader!
[    0.000000] Kernel panic - not syncing: Failed to allocate page table page
[    0.000000] CPU: 0 PID: 0 Comm: swapper Not tainted 6.8.0-111-generic #111~22.04.1-Ubuntu
[    0.000000] Call trace:
[    0.000000]  dump_backtrace+0xa8/0x158
[    0.000000]  show_stack+0x24/0x50
[    0.000000]  dump_stack_lvl+0x3c/0x140
[    0.000000]  dump_stack+0x1c/0x38
[    0.000000]  panic+0x3ac/0x448
[    0.000000]  early_pgtable_alloc+0xd4/0xf8
[    0.000000]  alloc_init_pud+0x74/0x268
[    0.000000]  create_kpti_ng_temp_pgd+0xa4/0x158
[    0.000000]  map_kernel_segment+0xa4/0x138
[    0.000000]  paging_init+0xf8/0x410
[    0.000000]  setup_arch+0x1a0/0x320
[    0.000000]  start_kernel+0x80/0x450
[    0.000000]  __primary_switched+0xc0/0xd0
```

## Reproduction

The accompanying `notes/zbm-repro/run.sh` script does the following end-to-end. Maintainers with `testing/setup.sh -a` already in their workflow can substitute their `zfsbootmenu-pool.img` as the base image and invoke just steps 1-3 + qemu boot — that's the canonical "use upstream's own test pool" path.

1. Build the ZBM container image natively on aarch64 from upstream `releng/docker/Dockerfile`, `XBPS_REPOS=https://repo-de.voidlinux.org/current/aarch64`, default `KERNELS="linux6.6 linux6.12 linux6.18"`.
2. Run with `config.yaml` (Components mode):
   ```yaml
   Global:
     ManageImages: true
     DracutFlags:
       - "--no-early-microcode"
   Components:
     Versions: false
     Enabled: true
   EFI:
     Versions: false
     Enabled: false
   Kernel:
     CommandLine: earlycon=pl011,0x9000000,115200 loglevel=7 console=tty0 console=ttyAMA0,115200
   ```
   plus the upstream `release.conf.d/common.conf` + `recovery.conf.d/recovery.conf` dracut overlays. (Adding `add_drivers+=" virtio_gpu efifb simplefb "` to `common.conf` doesn't change the failure mode.)
3. Provision an Ubuntu 22.04 jammy VM with `linux-generic-hwe-22.04` as the on-pool kernel, ZFS rpool at `rpool/ROOT/jammy`, ZBM's kernel + initrd installed under `/boot/efi/EFI/ZBM/`, rEFInd configured with:
   ```
   menuentry "Ubuntu (ZBM)" {
       loader /EFI/ZBM/vmlinux-bootmenu
       initrd /EFI/ZBM/initramfs-bootmenu.img
       options "earlycon=pl011,0x9000000,115200 loglevel=7 console=tty0 console=ttyAMA0,115200 zbm.show"
   }
   ```
4. Boot via:
   ```
   qemu-system-aarch64 \
     -accel hvf -machine virt -cpu host -smp 8 -m 8192 \
     -drive file=/opt/homebrew/share/qemu/edk2-aarch64-code.fd,if=pflash,unit=0,format=raw,readonly=on \
     -drive file=/path/to/efivars.fd,if=pflash,unit=1,format=raw \
     -drive file=/path/to/disk.qcow2,if=virtio,format=qcow2 \
     -device virtio-gpu-pci -device qemu-xhci -device usb-kbd \
     -netdev user,id=u0,hostfwd=tcp:127.0.0.1:2242-:22 -device virtio-net,netdev=u0 \
     -serial mon:stdio
   ```
5. ZBM UI appears, default-boots `rpool/ROOT/jammy`'s kernel via `kexec`, panics on alignment as above.

## What we tried, what didn't work

| Hypothesis | Test | Result |
|---|---|---|
| systemd-boot aarch64 EFI stub bug → switch to Components mode (no UKI) | Built with `EFI.Enabled: false`, `Components.Enabled: true`; rEFInd loads kernel + initrd directly | Got *past* the stub failure; ZBM now runs. Different bug appeared (this kexec one). |
| Old on-pool kernel (Ubuntu 5.15) lacks aarch64 kexec robustness | Installed `linux-generic-hwe-22.04` (6.8.0-111) | Identical panic, identical stack trace |
| Old kexec-tools shipped in Void's `current/aarch64` (2.0.32_1) misplaces image | Rebuilt kexec-tools from upstream `git.kernel.org/pub/scm/utils/kernel/kexec/kexec-tools.git` master (`2.0.32.git`) and `make install`'d over `/usr/sbin/kexec` in the builder image; verified via `strings`/`--version` that the rebuilt binary made it into the dracut initramfs | Identical panic |
| `kexec_load()` syscall (`kexec -l`) does the placement wrong, but `kexec_file_load()` (`kexec -s -l`) lets the kernel handle alignment | From ZBM's recovery shell: `kexec -s -l /mnt/boot/vmlinuz-6.8.0-111-generic --initrd=... --append='...'` then `kexec -e` | Identical panic |
| Insufficient RAM blocks alignment search | Bumped from 4 GiB to 8 GiB | Did clear an earlier `ConvertPages` error from EDK2 firmware on UKI loads, but no effect on the kexec panic |
| Initramfs builder difference (dracut vs mkinitcpio) somehow affects what kexec packages or how it runs | Switched ZBM build from dracut to mkinitcpio (`Global.InitCPIO: true` + upstream's reference `mkinitcpio.conf`) | Identical panic. The new kernel reaches earlycon and runs `efi: Can't find property 'linux,uefi-secure-boot' in DT!` before the misalignment panic — confirming the kernel image *is* being executed and the panic is purely about the load address. The initramfs isn't unpacked until later, so no initramfs-builder choice could affect this. |
| Source-kernel regression: 6.18 (ZBM's default kernel pick from `KERNELS="linux6.6 linux6.12 linux6.18"`) introduced an aarch64 kexec bug not present in 6.6 | Built ZBM with `-k 6.6` to force `linux6.6.*` as the kernel inside the bundle | Identical panic. So the bug isn't kernel-version-bounded on the *source* side either — same as on the destination side. |

## What does work

- Booting ZBM itself directly with `qemu-system-aarch64 -kernel vmlinux-bootmenu -initrd initramfs-bootmenu.img -append '...'`. ZBM bash runs, imports rpool, lists kernels.
- Booting a non-ZFS-rooted Ubuntu 22.04 jammy install (a `packer`-built `ubuntu-base` image) on the same aarch64 + EDK2 + QEMU virt combo via the standard firmware → `shimaa64.efi` → GRUB → kernel-EFI-stub path. Same `linux-image-generic` kernel binary that ZBM later tries to `kexec`. Boots fine, reaches userspace.
- **Booting the actual on-pool kernel + initrd directly via QEMU's `-kernel`/`-initrd` against the same qcow2 that ZBM is failing to kexec into:**
   ```
   qemu-system-aarch64 \
     -accel hvf -machine virt -cpu host -smp 8 -m 8192 \
     -kernel /tmp/vmlinuz-6.8.0-111-generic \
     -initrd /tmp/initrd.img-6.8.0-111-generic \
     -append 'earlycon=pl011,0x9000000,115200 console=ttyAMA0,115200 root=ZFS=rpool/ROOT/jammy ro' \
     -drive file=...edk2-aarch64-code.fd,if=pflash,unit=0,format=raw,readonly=on \
     -drive file=/tmp/efivars.fd,if=pflash,unit=1,format=raw \
     -drive file=/tmp/zfs-test.qcow2,if=virtio,format=qcow2 \
     -nographic
   ```
   Reaches userspace, login prompt, full system functional. Same physical kernel + initrd bytes that ZBM is failing to kexec — extracted from the rpool by entering ZBM's recovery shell and shipping them out via bash's `/dev/tcp` redirect.

So the kernel image is good, the rpool is fine, the firmware path can launch it, and **QEMU's own `-kernel` loader places it correctly**. The failure is specifically when the hand-off comes from `kexec` (either the user-space tool's `kexec_load()` or the kernel-side `kexec_file_load()`) inside an already-running aarch64 Linux kernel on this EDK2 firmware.

## Hypothesis / questions for upstream

1. The Linux arm64 architecture spec requires the kernel image be 2 MiB-aligned in physical memory at handoff. The error message is the kernel checking `_text` against `MIN_KIMG_ALIGN` and finding it misaligned. This is consistent with `kexec_load()` placing the new kernel at an address that doesn't meet alignment.

2. We confirmed `kexec_file_load()` (kernel-side, via `kexec -s`) **also** fails identically. That's surprising — the kernel-side image loader for arm64 is supposed to handle alignment itself (`arch/arm64/kernel/machine_kexec_file.c` / `arch_kexec_kernel_image_load`). Either the alignment logic isn't being applied, or a downstream stage (post-`start_kernel`) is what's tripping the misalignment check.

3. We see no upstream tests for ZBM on aarch64 + QEMU virt + EDK2 + Linux running ZFS. ZBM v3.1.0 added aarch64 *build* support but the kexec-from-running-aarch64-Linux-into-aarch64-Linux path doesn't appear to be exercised in CI.

Specific questions:

- Is aarch64 kexec under EDK2 + QEMU virt known to work for any of the ZBM developers? If so, what kernel/firmware/kexec-tools combo? (We'd be happy to switch to whatever tested combo exists.)
- Is there a "bootefi"/EFI-runtime-services-based handoff in any branch of ZBM that could replace kexec on aarch64? (Analogous to how systemd-boot and shim-aware loaders re-enter UEFI to load the next kernel.)
- Has the kexec misalignment we see been reported against `kexec-tools` or the kernel before, in the QEMU virt context specifically?

Layers we have **not** yet varied (out of scope for our local debugging):

- **EDK2 firmware build**: tested only the version Homebrew QEMU 9.2 bundles (`edk2-stable202408-prebuilt.qemu.org`, Sep 2024). Newer or differently-configured EDK2 might leave runtime services in a state more friendly to kexec.
- **QEMU acceleration backend**: tested only HVF on Apple Silicon. Have not tested TCG (pure software emulation) or KVM on a real arm64 host. If it works under TCG/KVM and not HVF, the bug narrows to HVF's interaction with EFI runtime services and kexec page-table teardown.
- **QEMU machine model**: tested only `virt`. Have not tested `sbsa-ref`.

## Workaround we're shipping

- **x86_64 prod hosts**: continue with the upstream `recovery` UKI v3.0.1, fetched and installed by chroot.sh / our ansible role. (No regressions.)
- **aarch64 development VMs (homelab on Mac)**: declared advisory. Build pipeline validated end-to-end (ZBM produces correct artifacts, Gitea hosts them, chroot.sh fetches them, rEFInd loads kernel + initrd, ZBM runs). The single remaining failure (kexec to the on-pool kernel) means we can't currently use aarch64 ZFS-rooted VMs for OS-level testing, only for bootloader-stack testing. Tracking via this report.

## Repo / artifacts

- Local repo: <homelab repo URL>
- Branch with all the aarch64-related work: `master` (commits `da45f1d` through `[latest]`)
- Components-mode config: `zbm/config.yaml`, `zbm/dracut.conf.d/`
- Builder Dockerfile (with kexec-tools-from-source RUN block): `zbm/Dockerfile`
- Iteration script (boot a built tarball directly): `test.sh`

We're happy to run any further diagnostics — e.g. dump kexec's planned memory layout via `kexec --debug -l ...`, attach a kgdb stub, run ftrace through the kexec call — if it'd help narrow this down.
