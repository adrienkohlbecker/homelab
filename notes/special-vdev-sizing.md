# Special metadata vdev for tank — sizing & benefit analysis

Date: 2026-05-05
Host: pug (homelab)
Question: the root NVMe drives have 3 × 128 GiB partitions reserved for an
eventual special metadata vdev on `tank`. Is that enough, and is it worth doing?

**Verdict:** yes, comfortably enough for metadata-only and for
`special_small_blocks` up to 32K. The workload would benefit meaningfully —
2.5M files on 4-disk raidz2 with a saturated ARC metadata cache is a textbook
fit. Caveat: on a raidz pool the special vdev cannot be removed, so it's a
one-way change.

---

## 1. Pool topology and current state

```
$ zpool status tank
  pool: tank
 state: ONLINE
  scan: scrub repaired 0B in 20:28:21 with 0 errors on Sun Apr 12 20:53:12 2026
config:

	NAME                                           STATE     READ WRITE CKSUM
	tank                                           ONLINE       0     0     0
	  raidz2-0                                     ONLINE       0     0     0
	    scsi-SATA_WDC_WD141KFGX-68_9LKWD6RG-part1  ONLINE       0     0     0
	    scsi-SATA_WDC_WD141KFGX-68_9LKW9SAG-part1  ONLINE       0     0     0
	    scsi-SATA_WDC_WD101EFBX-68_VH1R6JXM        ONLINE       0     0     0
	    scsi-SATA_WDC_WD101EFBX-68_VH1WHWKM        ONLINE       0     0     0

$ zpool list tank
NAME   SIZE  ALLOC   FREE  CKPOINT  EXPANDSZ   FRAG    CAP  DEDUP    HEALTH
tank  36.2T  18.2T  18.0T        -         -     2%    50%  1.00x    ONLINE
```

- 4-disk raidz2, 36.2T raw / **18.2T allocated / 8.84T logical** (50% full)
- ZFS 2.2.2-0ubuntu9.4, ashift=12
- No existing special / log / cache / dedup vdevs
- zstd compression everywhere, no dedup
- Default recordsize 128K, except `tank/media` at 1M
- 216 snapshots, daily zfs_autobackup activity

```
$ zfs list -r tank
NAME                             USED  REFER  RECSIZE  COMPRESS
tank                            8.84T   140K     128K  zstd
tank/brumath                    1.05T  1.05T     128K  zstd
tank/data                       2.60T  2.48T     128K  zstd
tank/eckwersheim                 638G   638G     128K  zstd
tank/media                      3.60T  3.60T       1M  zstd
tank/migration                   931G   931G     128K  off
tank/pug                        55.9G   140K     128K  zstd
…(pug rpool descendants under tank/pug)
```

## 2. Reserved partitions

```
$ sudo sgdisk -p /dev/nvme0n1
Disk /dev/nvme0n1: 117212886 sectors, 447.1 GiB
Model: KINGSTON SEDC1000BM8480G
Sector size (logical/physical): 4096/4096 bytes

Number  Start (sector)    End (sector)  Size       Code  Name
   1             256          131327   512.0 MiB   EF00      (ESP, mdraid)
   2          131328          259327   500.0 MiB   FD00      (swap, mdraid)
   3          259328        33813759   128.0 GiB   BE00      <-- reserved
   4        33813760       117212880   318.1 GiB   BF00      (rpool member)
   5               6             255   1000.0 KiB  EF02      (BIOS boot)
```

Identical layout on `nvme1n1` and `nvme2n1`.

- 3 × 128.0 GiB partitions (~137 GB decimal) on Kingston **DC1000B M.2** —
  data-center NVMe with PLP (the right class for a special vdev; consumer
  NVMe is not).
- All three partitions zeroed and unused (`blkid` shows only `PARTUUID`,
  `dd ... | od` is all NULs).
- A 3-way mirror gives **128 GiB usable**.
- SMART: `percentage_used: 4%` after 16,688 hours (~1.9 y). Plenty of life
  for a metadata-write workload.

```
$ sudo nvme smart-log /dev/nvme0n1 | grep -E 'percentage_used|power_on_hours|data_units_written'
data_units_written         : 145,783,339
power_on_hours             : 16,688
percentage_used            : 4%
```

## 3. Workload profile

### 3.1 File counts

```
/mnt/data        : 1,471,838 files
/mnt/media       :     4,152 files
/mnt/brumath     :   493,781 files
/mnt/eckwersheim :   289,111 files
/mnt/migration   :       246 files
                  ----------
total            : ~2.26M files
```

### 3.2 File-size distribution (logical bytes per dataset)

```
                <=4K     <=8K   <=16K   <=32K   <=64K  <=128K   <=1M    >1M
tank/data       50.1%   10.9%   5.9%    3.8%    5.0%    4.7%    8.2%   11.4%
tank/brumath    42.3%    2.9%   1.4%    0.7%    1.0%    7.7%   20.5%   23.5%
tank/eckwersheim 17.4%  11.2%  10.3%   10.3%    8.5%    6.8%   11.4%   24.1%
tank/migration  49.6%    0.4%   0.4%    0.4%    0.4%    0.4%    0.0%   48.4%
tank/media        ~all >1M (1M recordsize, big files)
```

Cumulative *logical* size of files at or below each threshold across the
three small-file datasets (data + brumath + eckwersheim):

| threshold | total small files | total bytes |
|-----------|------------------:|------------:|
| ≤4K       |  997 K            |   0.87 GB   |
| ≤8K       | 1.21 M            |   2.11 GB   |
| ≤32K      | 1.34 M            |   5.81 GB   |
| ≤64K      | 1.44 M            |  10.77 GB   |
| ≤128K     | 1.57 M            |  22.42 GB   |

(Note: at the file level. The block-level histogram from zdb is what actually
matters for `special_small_blocks`; see §4.)

### 3.3 ARC behaviour

```
$ awk '...' /proc/spl/kstat/zfs/arcstats
c_max                     8.00 GiB
data_size                 0.84 GiB
metadata_size             2.20 GiB
dnode_size                1.96 GiB
dbuf_size                 0.82 GiB
bonus_size                0.95 GiB
arc_meta_used             5.98 GiB     <-- 75% of 8 GiB ARC cap
```

ARC is currently spending **6 of its 8 GiB on metadata** (dnodes + dbufs +
bonus + indirect). Strong signal that the workload is metadata-heavy; the HDD
is being saved by warm cache, but cold metadata reads do hit the platters.

## 4. Ground truth from `zdb -bb tank`

50-minute pool traversal, no leaks reported.

### 4.1 Pool-wide block summary

```
bp count:              79,974,301
ganged count:             575,070
bp logical:        10,065,089,009,152   (~10.07 TB)
bp physical:        9,674,374,533,632   (~ 9.67 TB)  compression: 1.04
bp allocated:      20,029,147,631,616   (~18.21 TB)  compression: 0.50  <-- raidz2 parity
bp deduped:                       0
Normal class:      20,013,073,821,696   used: 50.56%
Embedded log class:    16,058,843,136   used:  5.84%

Dittoed blocks on same vdev: 1,948,584
```

### 4.2 Per-type breakdown (non-empty rows only)

```
Blocks    LSIZE   PSIZE   ASIZE     avg    comp   %Total   Type
     2     32K      8K      72K     36K    4.00    0.00   object directory
     3   1.50K       1K     72K     24K    1.50    0.00   object array
     1     16K      4K      36K     36K    4.00    0.00   packed nvlist
 4.05K    518M   41.6M    357M    88.1K   12.47    0.00   bpobj
   797   77.4M   48.8M    304M     391K    1.59    0.00   SPA space map
     4    280K    280K    600K     150K    1.00    0.00   ZIL intent log
  477K   7.72G   1.98G   11.8G    25.3K    3.89    0.06   DMU dnode
   134    532K    528K   3.15M    24.1K    1.01    0.00   DMU objset
    18   9.50K   1.50K     72K       4K    6.33    0.00   DSL directory child map
    16   30.5K     25K    288K      18K    1.22    0.00   DSL dataset snap map
    29    356K     88K    792K    27.3K    4.04    0.00   DSL props
 74.8M   9.14T   8.79T   18.2T     249K    1.04   99.85   ZFS plain file
  607K   4.94G    827M   6.60G    11.1K    6.11    0.04   ZFS directory
    14      7K      7K    336K      24K    1.00    0.00   ZFS master node
    81    658K     88K    528K    6.52K    7.48    0.00   ZFS delete queue
  164K   2.59G   1.26G   3.68G    22.9K    2.05    0.02   zvol object
   388   48.5M   5.85M   41.0M     108K    8.29    0.00   SPA history
   129     65K      1K     36K      285   65.00    0.00   DSL dataset next clones
   245    295K    231K   2.98M    12.4K    1.28    0.00   ZFS user/group/project used
  215K    254M    254M   5.03G    24.0K    1.00    0.03   System attributes
    14     21K     21K    336K      24K    1.00    0.00   SA attr registration
    28    448K    112K    672K      24K    4.00    0.00   SA attr layouts
   232    624K    600K   7.56M    33.4K    1.04    0.00   DSL deadlist map
     9      5K      1K     36K       4K    5.00    0.00   DSL dir clones
   122   15.2M    482K   4.29M      36K   32.40    0.00   bpobj subobj
   280   2.47M    288K   8.40M    30.7K    8.80    0.00   other
 76.3M   9.15T   8.80T   18.2T     245K    1.04  100.00   Total
 1.96M    101G   8.23G   50.8G    25.9K   12.29    0.27   Metadata Total
```

**Key line:**

> Metadata Total: 1.96M blocks, 101 G LSIZE, **8.23 G PSIZE**, **50.8 G ASIZE**

- LSIZE 101 G → PSIZE 8.23 G : metadata compresses ~12× under zstd.
- ASIZE 50.8 G is the **on-raidz2** cost today — PSIZE × ditto × parity overhead.
- Metadata is 0.27% of the pool by ASIZE, slightly under the 0.3% rule of thumb.

### 4.3 Block-size histogram (LSIZE cumulative — what `special_small_blocks` matches)

```
Block Size Histogram
  block   psize                lsize                asize
   size   Count   Size   Cum.  Count   Size   Cum.  Count   Size   Cum.
    512:   650K   325M   325M   650K   325M   325M      0      0      0
     1K:   420K   502M   827M   419K   501M   826M      0      0      0
     2K:   415K  1.02G  1.83G   414K  1.02G  1.82G      0      0      0
     4K:  1.73M  7.06G  8.88G   316K  1.70G  3.52G      0      0      0
     8K:   699K  6.46G  15.3G   251K  2.72G  6.25G  1.70M  20.4G  20.4G
    16K:   632K  13.5G  28.8G   927K  15.7G  21.9G  1.81M  43.6G  64.0G
    32K:  1.42M  67.0G  95.9G   161K  7.26G  29.2G   949K  41.6G   106G
    64K:  1.88M   167G   263G   164K  14.2G  43.4G  1.15M   109G   215G
   128K:  67.8M  8.47T  8.73T  72.3M  9.04T  9.08T  2.18M   388G   603G
   256K:     45  18.6M  8.73T      0      0  9.08T  67.8M  17.5T  18.1T
   512K:     32  22.7M  8.73T      0      0  9.08T    471   280M  18.1T
     1M:  70.5K  70.5G  8.80T  70.6K  70.6G  9.15T     32  45.5M  18.1T
     2M:      0      0  8.80T      0      0  9.15T  70.5K   141G  18.2T
```

LSIZE cumulative (the column that drives `special_small_blocks` decisions):

| threshold | total LSIZE ≤ threshold |
|-----------|-------------------------:|
| ≤4K       |   3.52 GB |
| ≤8K       |   6.25 GB |
| ≤16K      |  21.9 GB  |
| ≤32K      |  29.2 GB  |
| ≤64K      |  43.4 GB  |
| ≤128K     | 9.08 TB (everything) |

## 5. Special-vdev sizing projection

### 5.1 Why ASIZE on a mirror differs from ASIZE on raidz2

`bp allocated` (ASIZE) accounts for DVA × ditto × parity. Moving metadata to
a 3-way mirror changes the per-DVA cost from `PSIZE × (D+P)/D` (raidz2 with
small blocks ≈ 2-3×) to `PSIZE × 3` (mirror copies). Net effect: metadata
ASIZE roughly halves on a 3-way mirror compared to 4-disk raidz2.

- Now (50% pool fill): 50.8 GB raidz2 ASIZE → **~25 GB mirror ASIZE**
- 100% pool fill (linear projection): ~100 GB raidz2 → **~50 GB mirror**

### 5.2 Small-blocks cost on the mirror

Each block that lands on the special vdev is stored as 1 DVA × 3 mirror
copies = `3 × PSIZE`. PSIZE for the small-block tail is approximately 35-50%
of LSIZE (zstd compresses small text/config very well, but not all small
blocks are compressible).

Estimated mirror ASIZE for `special_small_blocks=N` at 100% pool fill,
assuming small-block volume scales linearly with pool fill:

| `special_small_blocks` | metadata @ full | small_blocks @ full | total | headroom on 128 GiB |
|-----------------------:|:---------------:|:-------------------:|:-----:|:--------------------:|
| 0 (metadata only)      | ~50 GB          | —                   | ~50 GB | huge |
| 16 K                   | ~50 GB          | ~25–40 GB           | ~75–90 GB | comfortable |
| 32 K                   | ~50 GB          | ~35–55 GB           | ~85–105 GB | workable |
| 64 K                   | ~50 GB          | ~60–95 GB           | ~110–145 GB | tight, may overflow |

Reminder: `special_small_blocks` must be **strictly less than recordsize**. With
the default 128 K recordsize, 64 K is the maximum legal value.

ZFS reserves 25% of the special vdev for metadata-only allocation
(`zfs_special_class_metadata_reserve_pct=25`). If small_blocks fill the rest,
metadata cannot be evicted but new small_blocks overflow back to raidz2 — no
breakage, just degraded benefit.

## 6. Will the workload actually benefit?

Yes:

- **Metadata-bound IOPS pattern.** 4-disk raidz2 has the random-IOPS profile
  of one disk. With ~2.3M files (50% of `tank/data` ≤ 4K), `ls -laR`, `find`,
  `du`, `rsync --dry-run`, snapshot listing, scrub-metadata-phase, and
  `zfs send` walks all hit platters. Mirror NVMe lifts those by 1-2 orders
  of magnitude.
- **ARC metadata saturation.** `arc_meta_used` 6 of 8 GiB confirms the cache
  is pushed; cold misses go to spinning rust today.
- **Small-block parity bloat.** A 4 K block on 4-disk raidz2 still consumes
  effectively 4 sectors; on a 3-way mirror it consumes 3. That's a small
  space win on top of the latency win.
- **Sequential reads on `tank/media` will not change.** This is a metadata
  fix, not a streaming-throughput fix. tank/media's 3.6 TB of 1-MB-record
  files barely participates.

Other zdb signals:

- **575 K ganged blocks** and **1.95 M dittoed-on-same-vdev blocks** — both
  typical of a metadata-heavy raidz2 starting to fragment. Both ease once
  metadata moves onto a dedicated vdev with abundant free space.
- 64 % fragmentation on `rpool` (separate pool, FYI) — unrelated, but worth
  watching.

## 7. Caveats

1. **One-way decision.** ZFS 2.2.2 does not allow `zpool remove` of a
   special vdev when raidz top-level vdevs are present. Adding it is
   permanent for this pool's lifetime.
2. **New failure domain.** Losing the 3-way mirror = losing tank. Three
   independent NVMes on the same host with PLP is reasonable, but it's now
   an additional critical dependency alongside the raidz2 parity disks.
3. **Don't set `special_small_blocks ≥ recordsize`.** Doing so routes
   *all* writes for the dataset onto the special vdev. Always strictly less.
4. **Existing data does not move.** `special_small_blocks` and metadata
   placement only apply to *new* writes. Migration requires a
   `zfs send | zfs recv` rewrite of the dataset (or just patience: COW will
   migrate hot blocks as they get rewritten).

## 8. Suggested rollout

1. **Add the special vdev (3-way mirror, by-id paths):**

   ```
   zpool add tank special mirror \
     /dev/disk/by-id/nvme-KINGSTON_SEDC1000BM8480G_50026B7686B5DE1D-part3 \
     /dev/disk/by-id/nvme-KINGSTON_SEDC1000BM8480G_50026B7686B5DE39-part3 \
     /dev/disk/by-id/nvme-KINGSTON_SEDC1000BM8480G_50026B7686B5DE54-part3
   ```

2. **Wait a few weeks** without enabling `special_small_blocks`. Watch
   `zpool list -v tank` — the special row will report its own ALLOC/FREE.
   Hot metadata will migrate naturally as it gets rewritten by snapshot
   churn / `zfs_autobackup`. Re-evaluate IOPS feel for `ls`, `find`,
   `zfs send` dry-runs.

3. **Optionally** enable small_blocks per-dataset, starting low:

   ```
   zfs set special_small_blocks=32K tank/data
   zfs set special_small_blocks=32K tank/brumath
   zfs set special_small_blocks=32K tank/eckwersheim
   # leave tank/media at 0
   # tank/migration is 246 files, doesn't matter
   ```

4. **Monitor over time.** Special vdev capacity should land around 30 GB
   for metadata at current pool fill, projecting to ~50 GB at full pool. If
   small_blocks usage approaches 80 GB combined, dial the threshold down or
   stop adding more.

## 9. Appendix — raw outputs preserved verbatim

### 9.1 `zpool list -v tank`

```
NAME                                            SIZE  ALLOC   FREE  CKPOINT  EXPANDSZ   FRAG    CAP  DEDUP    HEALTH
tank                                           36.2T  18.2T  18.0T        -         -     2%    50%  1.00x    ONLINE
  raidz2-0                                     36.2T  18.2T  18.0T        -         -     2%  50.2%      -    ONLINE
    scsi-SATA_WDC_WD141KFGX-68_9LKWD6RG-part1  9.10T      -      -        -         -      -      -      -    ONLINE
    scsi-SATA_WDC_WD141KFGX-68_9LKW9SAG-part1  9.10T      -      -        -         -      -      -      -    ONLINE
    scsi-SATA_WDC_WD101EFBX-68_VH1R6JXM        9.10T      -      -        -         -      -      -      -    ONLINE
    scsi-SATA_WDC_WD101EFBX-68_VH1WHWKM        9.10T      -      -        -         -      -      -      -    ONLINE
```

### 9.2 ZFS module parameters that affect special vdev

```
zfs_special_class_metadata_reserve_pct = 25
zfs_ddt_data_is_special                = 1
zfs_user_indirect_is_special           = 1
```

### 9.3 zdb `bp` summary (verbatim)

```
No leaks (block sum matches space maps exactly)

bp count:              79974301
ganged count:            575070
bp logical:      10065089009152      avg: 125854
bp physical:      9674374533632      avg: 120968     compression:   1.04
bp allocated:    20029147631616      avg: 250444     compression:   0.50
bp deduped:                   0    ref>1:      0   deduplication:   1.00
bp cloned:                    0    count:      0
Normal class:    20013073821696     used: 50.56%
Embedded log class    16058843136     used:  5.84%

additional, non-pointer bps of type 0:     675999
Dittoed blocks on same vdev: 1948584
Dittoed blocks in same metaslab: 1
```

### 9.4 NVMe target devices

```
nvme-KINGSTON_SEDC1000BM8480G_50026B7686B5DE1D   (nvme0n1, sn 50026B7686B5DE54 in id-ctrl)
nvme-KINGSTON_SEDC1000BM8480G_50026B7686B5DE39   (nvme1n1)
nvme-KINGSTON_SEDC1000BM8480G_50026B7686B5DE54   (nvme2n1)
```

(Verify mapping with `ls -l /dev/disk/by-id/nvme-* | grep part3` before running
`zpool add` — the by-id ↔ /dev/nvme* mapping is the bit you don't want to get
wrong.)

### 9.5 `arc_meta_used` snapshot

```
c_max                     8.00 GiB   (ARC max)
data_size                 0.84 GiB
metadata_size             2.20 GiB
dnode_size                1.96 GiB
dbuf_size                 0.82 GiB
bonus_size                0.95 GiB
arc_meta_used             5.98 GiB
```
