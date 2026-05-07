# macOS VM (`chi`) — design and tradeoff notes

Captures the research and design decisions for an always-on macOS Sequoia VM
on `lab`. Companion to `roles/macos_vm/` and `roles/macvlan/`. Pair with
`notes/lab-ram-freeup-plan.md` for the prerequisite host tuning.

## What we're building and why

An always-on macOS Sequoia guest on `lab`, used to:

- Keep an Apple ID signed in for iCloud Drive / iCloud Files access from
  remote machines (no Mac in the rack otherwise).
- Run remote Claude Code sessions over SSH.
- Occasional GUI use to set things up.

Not required: gaming, video editing, Metal-accelerated work, hardware video
decode. iMessage / FaceTime are nice-to-have but explicitly deferred (see
"iMessage / iCloud" below).

## Apple licensing

macOS's EULA permits running only on Apple hardware. A self-hosted lab VM
on commodity x86 is technically out-of-policy. Acceptable risk for personal
use; would not fit a commercial deployment. Mentioned for completeness;
no further accommodation in the role.

## Virtualization options surveyed

| Approach | Verdict |
|---|---|
| **OSX-KVM (kholia)** — QEMU/KVM + OpenCore EFI bootloader, defined as a libvirt domain | **Chosen.** Fits existing `roles/libvirt`. Most flexible, most-supported by community. |
| **Docker-OSX (sickcodes)** — wraps OSX-KVM in a privileged container | Rejected. Designed for one-shot desktop runs with X11; doesn't fit a server-managed always-on lab. |
| **macOS-Simple-KVM (foxlet)** | Rejected. Maintenance has lapsed, focuses on Mojave/Catalina. |
| **ULTMOS / ultimate-macOS-KVM (Coopydood)** | Considered. Higher-level wrapper; we want the libvirt XML directly to fit our Ansible patterns. Worth revisiting if upstream OSX-KVM stagnates. |
| **Apple Virtualization.framework** | Not applicable — host is Linux. |

## Host requirements (lab/i5-13500)

- **CPU**: Intel i5-13500 (Raptor Lake). AVX2 confirmed — Sequoia requires it.
- **Hybrid topology gotcha**: 6 P-cores + 8 E-cores = 20 threads, asymmetric.
  macOS expects homogeneous cores; mixing P/E threads causes scheduler
  weirdness and occasional hangs. **Mitigation**: pin VM vCPUs to P-core
  threads only via `cputune/vcpupin`. Defaults to `0,2,4,6` (first thread of
  each of 4 P-cores, no SMT contention). Verify with `lscpu -e` before
  applying — Linux numbers P-core threads first on Raptor Lake but it's
  worth a sanity check.
- **VT-d / IOMMU**: enabled and clean — every PCI device in its own group.
  Not strictly needed for block-level disk passthrough, but it's there if we
  ever add GPU passthrough.
- **AMD-specific tweaks** (`kvm.ignore_msrs=1`, etc.): N/A on Intel.

### RAM

- 30 GiB total, ~5.4 GiB free at investigation time.
- Sequoia + iCloud + a Claude session: **8 GiB minimum, 12 GiB target**.
- **`virtio-balloon` has no macOS driver** — Apple ships no kext, none
  exists in the community. Once the guest writes a page, the host can't
  reclaim it short of slow swap-out. Treat the VM's RAM allocation as
  effectively wired.
- → Free ~8 GiB on `lab` first. See `notes/lab-ram-freeup-plan.md`.

### Storage

#### Options surveyed

| Mode | Pros | Cons | Verdict |
|---|---|---|---|
| qcow2 file on `rpool/libvirt` | Easy, snapshots via either qemu or zfs, sparse | Tight on `rpool` (27 G free); double-COW (qcow2 + ZFS); double cache (ARC + qemu + macOS) | Skipped — rpool too tight |
| ZFS zvol on `dozer/vms/macos` | Native COW only, ZFS snapshots/clones/send, zstd compression, sparse, generous capacity | Vol sizing decisions; APFS+zvol needs `volblocksize=16K` for sane write-amp | Initial plan, superseded |
| **Block-level passthrough of physical SATA SSD** (`<disk type='block'>` → `/dev/disk/by-id/...`) | Disk is portable (pull → real Mac reads APFS); minimal QEMU overhead; no dozer space consumed; "feels like a real Mac" | No ZFS snapshots / send / compression / checksumming; one host locked to one disk; backups must be guest-side | **Chosen.** |
| NVMe PCIe passthrough | Native NVMe driver in macOS, full TRIM | Spare disk is SATA, not NVMe; would require buying NVMe | N/A |
| SATA controller PCIe passthrough | Bare-metal "real Mac" feel; helps iMessage activation | All 6 SATA disks (`dozer` mirror + `tank` raidz2) live on the single chipset SATA controller (group 7) — passing it through would yank `dozer`/`tank` from the host | **Ruled out** by topology |

#### Final storage plan

- **Spare 500 G SATA SSD plugged into a free chipset port** on the W680-ACE
  (≥2 ports free; physically install when ready).
- Domain XML uses `<disk type='block'>` pointing at
  `/dev/disk/by-id/ata-<MODEL>_<SERIAL>` — stable across reboots and
  port-shuffles.
- **OpenCore.qcow2** lives separately as a tiny qcow2 (~19 MB) on
  `rpool/libvirt` under `/var/lib/libvirt/images/macos-vm/<vm>/`. This file
  holds OpenCore NVRAM (incl. iCloud activation tokens) and must NOT be
  overwritten once initialized. The role copies a pristine pinned copy
  from `_artifacts/` into the per-VM workdir on first run only (`force:
  false`).
- **No ZFS snapshots** for the macOS disk — backups via Arq (guest-side) to
  an SMB or SFTP share on `tank` (8.5 TiB). Time Machine to the same share
  as belt-and-braces. macOS's automatic local APFS snapshots stay.

#### Disk permissions

The libvirt-qemu user (group `kvm`) needs read/write on the passthrough
block device. The role installs a udev rule that matches the disk by
`ID_SERIAL` and chowns to `libvirt-qemu:kvm`, mode 0660. This survives
reseats and reboots. Apparmor on Ubuntu's libvirt should auto-allow the
disk because it's declared in the domain XML; if not, add the path to
`/etc/apparmor.d/libvirt/TEMPLATE.qemu`.

## iCloud and iMessage

### iCloud Drive (chosen)

Works with any plausible OpenCore SMBIOS — just sign in with the Apple ID
+ 2FA. `iMacPro1,1` board profile is fine. No special setup. **This covers
the "always on for iCloud Files access" requirement.**

### iMessage / FaceTime (deferred)

Substantially harder. Needs:

- A **GenSMBIOS-generated serial that returns "not validated"** on Apple's
  coverage check (proves no real-Mac collision). Regenerate until clean.
- **MLB + Board Serial** paired to the same Mac generation.
- **ROM** field set to a real-looking MAC address (a `Data` blob in
  OpenCore's `PlatformInfo`).
- **Working NVRAM** — OpenCore.qcow2 with `RequestBootVarRouting` already
  handles this.
- **`en0` flagged "built-in"** — Hackintool fixup or `NullEthernet.kext`.
- **Apple ID hygiene**: a fresh ID with no purchase history gets blacklisted
  after a couple of failed activation attempts. Use an ID that already has
  another Apple device or App Store activity. If you blow it, you call
  Apple to unblock.

Plan: ship without iMessage. Generic SMBIOS in OpenCore, iCloud Drive only.
Add iMessage later if needed via a separate, opt-in pass that swaps the
`PlatformInfo` for GenSMBIOS-generated values.

## GPU / display

### Acceleration options

| Path | Status on i5-13500 |
|---|---|
| Intel iGPU (UHD 770) passthrough | Not viable. macOS dropped Intel iGPU support post-Comet-Lake; UHD 770 has no working driver. Plus you can't passthrough the only display device without losing host video. |
| NVIDIA discrete | Blocked since Mojave (10.14). No driver for any modern macOS. Dead. |
| **AMD discrete (RX 5000 / RX 6000 / older Polaris/Vega)** | **Only working path for hardware acceleration.** Best community results: **RX 6600 / 6600 XT / 6700 XT** (RDNA 2). RDNA 3 (RX 7000) has no driver and won't work. |
| virtio-vga + VNC software rendering | Default; what we're using. |

### What we're shipping

**Software rendering only.** No discrete GPU. Acceptable for the stated use
case (iCloud Files, Terminal/Claude over SSH). Tradeoffs:

- Initial install via QEMU's built-in VNC — laggy, ugly cursor, but works.
- Post-install: enable macOS Screen Sharing (port 5900) on the guest and
  connect from a client over the LAN via macvtap. Native ARD protocol,
  60 fps, retina cursor. Major UX upgrade over QEMU VNC.
- No Metal, no hardware video decode, sluggish window animations.

### If acceleration becomes necessary later

Buy an **AMD RX 6600 (~€150 used)**. Steps would be:

1. Install card in a free PCIe x16 slot.
2. Verify own IOMMU group on the W680-ACE (likely fine — workstation chipset).
3. Add `vfio-pci` binding via kernel cmdline + initramfs.
4. Add a `<hostdev>` block to the domain XML for the GPU + its HDMI audio
   function.
5. Switch the `<video>` element to `none`.
6. Keep the iGPU as the host's display.

Not in scope for now. Role can grow a `macos_vm_gpu_passthrough_pci_id` toggle.

## Networking

### Options

- **libvirt NAT bridge** (existing pattern, used by other VMs on lab).
  Guest gets `10.123.48.x`; reachable from lab; needs port-forwarding for
  LAN access.
- **macvtap "direct" mode** (chosen). Guest gets a real DHCP lease on the
  home LAN (10.123.0.0/23), addressable as a peer from any other LAN host.
  Lower latency, no NAT.
- macvlan parent ↔ child isolation: the kernel intentionally prevents the
  parent NIC and its macvtap children from talking to each other.
  Workaround: a macvlan child *on the host itself* (`mac0@eth0`) gives the
  host its own MAC + IP on the LAN that can reach the guest. Resurrected
  `roles/macvlan/` does exactly this.

### macvlan layout (to set in `host_vars/lab.yml`)

```yaml
macvlan_parent: eth0                    # primary NIC, already DHCP-up
macvlan_parent_subnet: 10.123.0.0/23    # host LAN
macvlan_parent_gateway: 10.123.0.1
macvlan_subnet: 10.123.1.0/27           # range carved out for this host's macvlan use (32 IPs)
macvlan_host_ip: "{{ macvlan_subnet | ansible.utils.ipmath(1) }}"  # convention from group_vars
macvlan_host_mac: "{{ '82:48:10' | random_mac(seed=inventory_hostname) }}"
```

By convention (group_vars/all.yml), `macvlan_host_ip` is always the first
usable IP of `macvlan_subnet` — for `.0/27` that's `.1`. The role asserts
this so `tasks_from: podman` can rely on it.

The macOS VM's MAC is generated deterministically from
`inventory_hostname + macos_vm_name` so it's stable across redefinitions.

### Subnet carving inside `macvlan_subnet`

`podman_network`'s `ip_range` must be a single aligned CIDR — there's no
way to express "all of macvlan_subnet except the host IP" as one CIDR. So
the macvlan role splits `macvlan_subnet` in half:

```
macvlan_subnet                    e.g. 10.123.1.0/27 (.0-.31)
  ├── lower /28 (.0-.15)          host (mac0 at .1) + libvirt VMs
  └── upper /28 (.16-.31)         podman containers (auto-assigned ip_range)
```

- **Lower half**: contains `macvlan_host_ip` at `.1`. Pick libvirt VM
  static IPs from `.2`–`.15` if you need them deterministic, or let LAN
  DHCP allocate (macvtap puts the guest on the home network so any free
  LAN IP works — they don't have to live in `macvlan_subnet`).
- **Upper half**: handed to podman as `ip_range`. Containers get IPs
  from this half automatically.

Cost: 50% of `macvlan_subnet`'s IPs reserved per side. Size
`macvlan_subnet` to 2× the larger of (VMs + host, container count).

### Optional: containers on macvlan

Roles that want to run podman containers on the same LAN can opt in by
importing the macvlan role's `podman` task:

```yaml
- import_role:
    name: macvlan
    tasks_from: podman
```

That creates a `macvlan_net` podman network with `subnet =
macvlan_parent_subnet`, `ip_range` = upper half of `macvlan_subnet`,
parent = the host NIC. Not used by `roles/macos_vm` (libvirt's macvtap
doesn't go through podman) — kept around for future uses, replacing the
deleted docker.yml.

### No Tailscale

Earlier iterations assumed Tailscale on the guest as the cross-LAN reach
solution. Dropped per user preference. macvlan host-side `mac0` interface
provides lab→guest connectivity.

## SMBIOS / OpenCore artifacts

`OpenCore.qcow2` from kholia/OSX-KVM ships a working OpenCore EFI partition
with the AppleVirtIO* kexts so the guest can boot from `virtio-blk`. It's
pinned by commit + sha256 in `roles/macos_vm/vars/main.yml`.

**Bumping the pin**: only do this during a planned re-install. Overwriting
the OpenCore.qcow2 in the per-VM workdir resets NVRAM, which kicks the
guest out of iCloud and clears any iMessage activation. Bumps should be
rare; the OpenCore config in upstream changes maybe yearly.

`fetch-macOS-v2.py` is also pinned at the same commit. It's used once per
VM during install to download `BaseSystem.dmg` (~700 MB) for the Recovery
installer. After install, the recovery DMG can be removed by flipping
`macos_vm_install_mode: false`.

## Role design

Two roles, separate concerns:

### `roles/macvlan/`

Resurrected from commit `dda8bdf^` (deleted 2026-05-04 because the docker
consumer was retired). Reinstated without the docker bits. Provides a
`mac0@<parent>` interface on the host so it can reach macvtap guests.
General-purpose; any future guest using macvtap can reuse it.

Tasks: install iproute2, validate vars, install + start a oneshot systemd
unit (`mac0.service`) that creates the interface on boot.

### `roles/macos_vm/`

Per-VM identity in host_vars; defaults in `defaults/main.yml`. Idempotent:
- Validate inputs (assert `macos_vm_disk_id` set, etc.).
- Compute a stable MAC from inventory + VM name.
- Cache OpenCore.qcow2 + fetch-macOS-v2.py with sha256-pinned `get_url`.
- One-time download of BaseSystem.dmg via `creates:` sentinel.
- Install a udev rule for the passthrough disk's permissions.
- Render and `define` the libvirt domain.
- Set `autostart`.

Intentional non-actions:

- **Does not start the VM.** First boot is interactive (Recovery installer).
  After install, set `macos_vm_install_mode: false` and re-run, then
  `virsh start chi` once.
- **Does not modify host CPU governor or other tunables.** Lives in `lab`'s
  existing groove.
- **Does not touch ZFS.** All storage is on the passthrough SSD.

## Operational runbook

### Phase 1 — host prep

1. Free ~8 GiB on `lab` per `notes/lab-ram-freeup-plan.md`.
2. Plug in the spare 500 G SATA SSD on a free chipset port.
3. `ssh lab 'ls -la /dev/disk/by-id/ | grep ata-'` — note the new entry.

### Phase 2 — wire vars

In `host_vars/lab.yml`, add:

```yaml
# macvlan (resurrected role) — host_ip / parent_* / parent_gateway / host_mac
# all default from group_vars/all.yml (restore the deleted block first).
# Per-host, only macvlan_subnet needs setting:
macvlan_parent: eth0
macvlan_subnet: 10.123.1.0/27   # /27 → lower /28 for VMs (host at .1), upper /28 for podman

# macOS VM
macos_vm_disk_id: ata-<MODEL>_<SERIAL>     # from step 1.3
macos_vm_cpu_pinset: "0,2,4,6"             # P-core threads on i5-13500 (verify with lscpu -e)
macos_vm_install_mode: true
macos_vm_autostart: false
```

In `site.yml`, add `macvlan` and `macos_vm` to the play targeting `lab`
(or whichever group is appropriate).

### Phase 3 — first run

```
ansible-playbook site.yml -l lab --tags macvlan,macos_vm --check    # dry-run
ansible-playbook site.yml -l lab --tags macvlan,macos_vm
```

### Phase 4 — install macOS

```
ssh -L 5900:127.0.0.1:5900 lab
virsh start chi
```

Connect a VNC client to `localhost:5900`. From OpenCore's boot picker, pick
the BaseSystem entry. In Recovery:

1. Disk Utility → erase the 500 G disk → APFS, GUID Partition Map, name it.
2. Quit Disk Utility, choose "Reinstall macOS Sequoia".
3. Pick the freshly-erased disk. ~20-40 minutes (downloads installer over
   the network).
4. The VM reboots a couple of times during install; pick the right
   OpenCore entry each time (it remembers via NVRAM after a couple).

### Phase 5 — post-install

In macOS:

- System Settings → General → Sharing → enable **Screen Sharing**.
- Sign in to Apple ID (iCloud Drive will sync).
- Install Arq, point at SMB share on `tank`.

In `host_vars/lab.yml`:

```yaml
macos_vm_install_mode: false
macos_vm_autostart: true
```

Re-run the role. The recovery DMG drops out of the domain XML and the VM
will start on host boot.

### Growing the disk

The whole disk is the macOS volume — to grow, replace with a larger SSD:

1. Power off the VM.
2. `dd` (or `zfs send` if it were a zvol) the old disk to the new one.
3. Plug in the new disk, update `macos_vm_disk_id`, re-run role.
4. Inside macOS: `diskutil apfs resizeContainer disk0s2 0`.

Clone-then-grow is the cleanest path because there's no zvol to resize.

## Loose ends and future work

- **Disk image-level backups** beyond Arq. Could `dd` the SATA SSD to a
  qcow2 on `tank` weekly via `cron`. Adds a "fall back to a real Mac with
  this disk" recovery path. Worth revisiting once the VM has been running
  a while.
- **iMessage**, if it becomes useful enough. Plumbing exists in OpenCore;
  needs SMBIOS regeneration + `en0`-built-in fixup + careful Apple ID
  selection.
- **GPU passthrough** with an AMD RX 6600 if the software-rendered UX
  becomes a daily friction point.
- **AppArmor profile** for the libvirt-qemu user accessing
  `/dev/disk/by-id/...`. The default Ubuntu profile may auto-allow when
  the disk is declared in domain XML; verify on first run, drop in a
  custom rule if not.
- **Test harness coverage** — `roles/macos_vm` is currently untested by
  `test/testrole.py`. End-to-end testing of a macOS install is impractical
  in CI, but the role's idempotency (artifacts, udev rule, domain-define
  re-runs) could be exercised against a `qemu_test` host that stops short
  of `virsh start`.
