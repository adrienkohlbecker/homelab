# Free up ~8 GiB on `lab` for an always-on macOS VM

## Goal

Make room on `lab` to run a macOS Sequoia KVM guest with **12 GiB allocated**.
Currently `lab` has 30 GiB total / ~5.4 GiB free, and `virtio-balloon` has no
macOS driver, so once the guest wires its RAM the host can't easily reclaim it.
Need to recover at least 8 GiB without losing services.

## Pre-work — check before doing anything else

1. **`zdb -bb tank` may be running** (PID was 1330534 at 2026-05-05 ~10:00,
   started by a manual session). It traverses the whole pool and balloons to
   multi-GiB. If unintentional, kill it before tuning anything:

   ```
   ssh lab 'pgrep -af "zdb -bb"'
   ssh lab 'sudo kill <pid>'   # if not yours
   ```

2. **Swap is fully consumed** (1.5 GiB / 1.5 GiB on `md127`) and `vmstat` shows
   ~50% iowait. Host is already paging. Add swap before bringing up the VM.

## Where the 22 GiB is going (snapshot, may have shifted)

| Bucket | Size | Notes |
|---|---|---|
| ZFS ARC | 8.0 GiB | At `c_max` ceiling |
| Nexus (JVM in container) | 3.0 GiB | `-Xmx4G -XX:MaxDirectMemorySize=2g` |
| OpenProject | 2.4 GiB | Rails/puma |
| Netdata | 1.8 GiB | `ebpf.plugin` alone is 305 MiB |
| HomeAssistant | 815 MiB | |
| Paperless (gunicorn+celery) | 700 MiB | |
| nginx | 619 MiB | |
| User shell session (ak) | 1.0 GiB | Ruby/bundler ~2.5 GiB total + claude 360 MiB |
| ~25 other containers | <300 MiB each | ~2 GiB combined |
| Slab SUnreclaim | 4.9 GiB | Overlaps ARC (mostly ARC headers) |

Total **AnonPages: 12.5 GiB + Shmem: 3.0 GiB + ARC: 8 GiB**. ARC is only ~36 %
of the used memory — ARC tuning alone does NOT get to 12 GiB free.

## Plan: recover ~8.3 GiB

All four together. Each is low-risk individually.

### 1. Lower ZFS ARC ceiling (-5 GiB)

ARC normally shrinks under pressure, but only down to `c_min` (currently 4 GiB).
That's why it's not freeing despite the host paging. Lower both:

- Persistent — write `/etc/modprobe.d/zfs.conf`:
  ```
  options zfs zfs_arc_max=3221225472    # 3 GiB
  options zfs zfs_arc_min=1073741824    # 1 GiB
  ```
- Live (no reboot): `echo 3221225472 | sudo tee /sys/module/zfs/parameters/zfs_arc_max`
  and same for `zfs_arc_min`. ARC will trim within ~30 s under pressure.
- Update initramfs: `sudo update-initramfs -u -k all` (because `/` is on ZFS
  and the module is loaded from initrd).

**Floor caveat**: `arc_meta_used` is 3.5 GiB and `dnode_cache` is 635 MiB.
Don't drop `arc_max` below ~2.5 GiB or metadata gets evicted and `find`,
snapshots, scrubs slow dramatically. 3 GiB is a safe target.

### 2. Cap netdata (-1.3 GiB)

Either:

- Drop-in `/etc/systemd/system/netdata.service.d/memory.conf`:
  ```
  [Service]
  MemoryMax=512M
  ```
  then `sudo systemctl daemon-reload && sudo systemctl restart netdata`.
- Or disable `ebpf.plugin` in `/etc/netdata/netdata.conf` if eBPF metrics
  aren't valuable. Saves 305 MiB by itself.

This repo manages netdata via the `netdata` role — change should land there,
not as a manual edit. Look at `roles/netdata/`.

### 3. Shrink Nexus JVM (-2 GiB)

Repo has a `nexus` role. Likely a templated `nexus.vmoptions` or env-var.
Change to:

- `-Xmx2g`
- `-XX:MaxDirectMemorySize=1g`

GC pressure goes up, fine unless you're heavily ingesting/scanning artifacts.
Verify Nexus stays healthy after the bounce (Sonatype healthcheck endpoint).

### 4. Add real swap (no GiB freed, but unblocks paging)

Current swap is 1.5 GiB on a small md127 partition, fully used. Add a
zvol-backed swap on `dozer` (mirror SSDs, fast):

```
sudo zfs create -V 16G \
  -b 4096 \
  -o compression=zstd \
  -o logbias=throughput \
  -o sync=always \
  -o primarycache=metadata \
  -o secondarycache=none \
  -o com.sun:auto-snapshot=false \
  dozer/swap
sudo mkswap -f /dev/zvol/dozer/swap
sudo swapon /dev/zvol/dozer/swap
```

Persist in `/etc/fstab`:

```
/dev/zvol/dozer/swap none swap defaults,nofail,x-systemd.requires=zfs-volumes.target 0 0
```

Lets the kernel shed cold anon pages to disk so the qemu guest can wire its
allocation without OOM-killing something.

**Caveat**: ZFS-on-zvol-swap has a historical deadlock risk under extreme
memory pressure (kernel needing memory to write to the swap that's backed by
a filesystem that needs memory). Mitigated by `primarycache=metadata`,
`logbias=throughput`, and not running it as the *only* swap. The chance is
low on a host with this much idle CPU; mention if you're nervous.

## Verification after changes

```
ssh lab 'free -h && echo --- && cat /proc/spl/kstat/zfs/arcstats | grep -E "^c |^c_max|^c_min|^size " && echo --- && systemd-cgtop -n 1 -m --depth=2 | head -15 && echo --- && swapon --show'
```

Expected: ~13–14 GiB free, ARC at 3 GiB, netdata <600 MiB, Nexus ~2 GiB,
swap at 17.5 GiB total.

## Out of scope (intentionally)

- Killing/disabling services (HomeAssistant, Paperless, OpenProject) — all
  in active use.
- Reducing the user shell session footprint (Ruby/bundler, claude) — that's
  ephemeral and will free naturally.
- Migrating containers off `lab` — much bigger project.

## After this is done

The macOS VM role (`roles/macos_vm`, not yet written) will be sized for
12 GiB / 4 vCPUs and pinned to P-core threads. Once RAM is freed, plug in
the spare 500 GB SATA SSD and capture `/dev/disk/by-id/ata-<MODEL>_<SERIAL>`
to set as `macos_vm_disk_id` host_var.
