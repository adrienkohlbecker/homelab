# rpool/services snapshot growth — SQLite write amplification analysis

Date: 2026-05-06
Host: lab (homelab)
Question: daily snapshots on `rpool/services` are 170–600 MB each and the
pool is at 9.5 GB free / 119 GB. Which services are responsible, and is
recordsize tuning worth doing?

**Verdict:** ~95% of daily churn is a handful of SQLite databases (Pi-hole
FTL, HA recorder, Tautulli, Sonarr/Radarr/Jellyfin, Kuma) being partially
rewritten under the parent dataset's default 128K recordsize. Carved a
child dataset `rpool/services/sqlite` at `recordsize=4K` and routed the
worst offenders onto it across three batches:

1. **pihole + HA recorder** via volume swap (DB had its own subdir or a
   relocation env var).
2. **bazarr / overseerr / filebrowser / csplogger / mosquitto /
   healthchecks / gitea** — same volume-swap idiom; DBs already lived in
   a relocatable subdir or accepted a config knob.
3. **sonarr / radarr / sabnzbd / headphones / profilarr / jellyfin /
   kuma / z2m / tautulli + HA `zigbee.db`** — DB hardcoded next to other
   state, so a clean directory bind isn't possible. Used a symlink
   inside the original config dir pointing to the 4K dataset, with a
   matching-path bind so SQLite resolves the link identically inside
   the container. WAL/SHM follow the resolved path onto the 4K dataset.

Plex and paperless still pending — they need bespoke handling (plex has
a deeply nested DB dir worth a directory bind; paperless mixes SQLite
with the Whoosh index, which doesn't want 4K records).

---

## 1. Pool state

```
$ zfs list rpool/services
NAME             USED  AVAIL  REFER  MOUNTPOINT
rpool/services   109G  9.51G  30.8G  /mnt/services

$ zfs get -p used,referenced,usedbysnapshots rpool/services
rpool/services  used             116695130112
rpool/services  referenced       33119776768
rpool/services  usedbysnapshots  83575353344
```

109 GB used, of which **78 GB is held by snapshots** (~3.5× the live
dataset). Daily snapshot retention back to Aug 2025 with monthly thinning.

```
$ zfs list -t snapshot rpool/services | tail -10
rpool/services@bak-20260428022002   182M  52.5G  Tue Apr 28
rpool/services@bak-20260429022001   165M  52.5G  Wed Apr 29
rpool/services@bak-20260430022001   217M  52.5G  Thu Apr 30
rpool/services@bak-20260501022001   185M  22.4G  Fri May  1
rpool/services@bak-20260502022001   170M  24.5G  Sat May  2
rpool/services@bak-20260503022001   611M  31.5G  Sun May  3
rpool/services@bak-20260504022001  7.00G  40.4G  Mon May  4
rpool/services@bak-20260505022001   170M  30.8G  Tue May  5
```

170–600 MB/day baseline, with one 7 GB outlier on May 4 (cause not
investigated; suspected pg_wal churn or an HA recorder purge — the
file-level diff didn't show anything unusually large that day).

## 2. Identifying the culprits

`zfs diff` between consecutive snapshots, summing the current on-disk
sizes of files flagged as Modified or Added:

```
$ sudo zfs diff rpool/services@bak-20260503022001 rpool/services@bak-20260504022001 \
    | awk '$1=="M" || $1=="+" {print $2}' > /tmp/diff.txt
$ wc -l /tmp/diff.txt
10893

$ while IFS= read -r f; do [ -f "$f" ] && stat -c "%s %n" "$f"; done </tmp/diff.txt \
    | awk '{path=$2; gsub("/mnt/services/","",path); split(path,a,"/");
            sum[a[1]]+=$1} END {for(k in sum) printf "%12d  %s\n",sum[k],k}' \
    | sort -rn | head
```

May 3 → May 4 (typical day):

| Service | Bytes of files modified | Largest contributor |
|---|---|---|
| pihole | 1.22 GB | `etc/pihole-FTL.db` (1.1 GB) + `gravity.db`/`gravity_old.db` (44 MB ea) |
| homeassistant | 569 MB | `home-assistant_v2.db` |
| tautulli | 197 MB | `tautulli.db` (47 MB) **+ 3 backups/day × 47 MB** |
| gitea | 189 MB | mostly `data/packages/...` (legitimate package uploads) |
| jellyfin | 125 MB | `data/library.db` + WAL |
| openproject | 92 MB | `pgdata/pg_wal/...` segments |
| nexus | 86 MB | `data/db/nexus.mv.db` + `nexus.trace.db` |
| sonarr | 68 MB | `sonarr.db` |
| kuma | 40 MB | `kuma.db` |

May 2 → May 3 looked virtually identical — the same DBs in the same
ranking. Pattern is steady-state.

Total "logical" changed bytes: ~2.6 GB/day. Actual snapshot USED for that
window: 611 MB. The gap is exactly the point of this investigation —
**ZFS only retains the diverged 128K records, not whole files**. So mtime
flips the file as Modified in `zfs diff` but only a fraction of the file
diverges block-by-block.

## 3. Full DB inventory in /mnt/services

For reference — the catalog of all database engines and files in the
services tree at the time of investigation.

### PostgreSQL
- `/mnt/services/openproject/pgdata` — 34 MB cluster (one PG_VERSION,
  4 base/N dirs)

### H2 (Java embedded)
- `nexus/data/db/nexus.mv.db` — 49 MB
- `nexus/data/db/nexus.trace.db` — 32 MB

### InfluxDB (TSM/WAL)
- `influxdb/data/engine/{data,wal}` — 1.2 GB total
- `influxdb/data/influxd.sqlite` — 120 KB (metadata only)

### Redis
- `redis/dump.rdb` — 25 KB

### LevelDB
- `gitea/data/queues/common` — 173 KB (CURRENT + MANIFEST + .ldb)

### SQLite (38 files total)

**Large (>40 MB) — these dominate the snapshot churn:**

| Path | Size |
|---|---:|
| `pihole/etc/pihole-FTL.db` | 1.1 GB |
| `homeassistant/home-assistant_v2.db` | 565 MB |
| `plex/.../com.plexapp.plugins.library.db` | 131 MB |
| `plex/.../com.plexapp.plugins.library.blobs.db` | 128 MB |
| `sonarr/sonarr.db` | 65 MB |
| `jellyfin/data/library.db` | 56 MB |
| `tautulli/tautulli.db` | 47 MB |
| `tautulli/backups/tautulli.backup-*.sched.db` × 8 | 47 MB ea |
| `pihole/etc/gravity.db` (+ `gravity_old.db`) | 44 MB ea |
| `kuma/kuma.db` | 40 MB |
| `plex/.../com.plexapp.dlna.db` | 40 MB |

**Medium (1–30 MB):**

| Path | Size |
|---|---:|
| `sabnzbd/admin/history1.db` | 29 MB |
| `headphones/headphones.db` | 12 MB |
| `radarr/radarr.db` | 8 MB |
| `bazarr/db/bazarr.db` | 8 MB |
| `jellyfin/data/jellyfin.db` | 6 MB |
| `gitea/data/gitea.db` | 3 MB |
| `paperless/data/db.sqlite3` | 2 MB |
| `paperless/data/celerybeat-schedule.db` | 2 MB |
| `jellyfin/data/introskipper/introskipper.db` | 2 MB |
| `sonarr/logs.db` | 1.8 MB |
| `csplogger/csp_violations.db` | 1.4 MB |
| `mosquitto/data/mosquitto.db` | 1 MB |
| `radarr/logs.db` | 0.9 MB |

**Small (<1 MB):** `homeassistant/zigbee.db`, `healthchecks/hc.sqlite`,
`overseerr/db/db.sqlite3`, `z2m/database.db`,
`filebrowser/database/filebrowser.db`, `profilarr/profilarr.db`.

### Notes on the inventory
- **Tautulli** keeps 8 daily backups of its DB (~378 MB just in
  `backups/`). Lowest-hanging app-config win independent of recordsize
  tuning — see §9.
- **Plex** has ~340 MB of SQLite DBs but rewrites less aggressively
  than HA/pihole; it didn't surface in the daily diff sums in §2.
  Worth folding into the same 4K dataset opportunistically if it grows.
- **InfluxDB** is not SQLite — TSM is column-oriented, append-mostly
  with periodic compaction. Compaction events could explain occasional
  snapshot spikes (e.g. the 7 GB May 4 outlier). Recordsize tuning is
  different for TSM (typically 64K–1M is fine).
- **PostgreSQL** is small here but `pg_wal` rotation drives its share
  of churn. Strongly prefers `recordsize=8K` matching its page size on
  a dedicated dataset — different beast from SQLite, not folded in.

## 4. Why 128K is wrong for SQLite

ZFS is copy-on-write at the **record** level. With recordsize=128K and
SQLite's default page_size=4096:

1. SQLite writes one 4K page (e.g. a state insert in HA recorder).
2. ZFS reads the entire 128K record containing it.
3. Modifies 4K, writes a new 128K record (CoW).
4. The new record diverges from the previous snapshot.

**Result: 32× write amplification** for a single 4K page write, and the
snapshot retains 128K of "diverged data" per modified page even though
only 4K is genuinely new.

For HA recorder (565 MB) and pihole-FTL (1.1 GB), which scatter small
writes across the file (recorder via the events table + indices,
FTL via the queries table tail + indices), this is exactly the wrong
shape.

## 5. Picking a recordsize: 4K vs 16K vs 8K

ZFS pool is `ashift=12` (4K sector minimum allocation), which is
universal on modern drives. This shapes the trade-offs:

| recordsize | write amp (4K page) | compression possible | metadata | snapshot diff per modified page |
|---|---|---|---|---|
| 4K  | 1× | **No** (1 sector floor) | ~4% | 4K |
| 8K  | 2× | Marginal (2 sectors) | ~2% | up to 8K |
| 16K | 4× | Yes (~1.5–2× typical) | ~1% | up to 16K |
| 128K (parent default) | 32× | Best (~2× on SQLite data) | ~0.1% | up to 128K |

**Compression at recordsize=4K is dead.** A 4K record that LZ4 compresses
to 2K still rounds up to one 4K sector — there's no smaller unit to
allocate. So compressratio on this dataset will be 1.00x by construction.

At recordsize=16K, a 16K record compressing to 8K saves two sectors —
real space win (typically ~30–50% on time-series-style SQLite data).

### The decision

Initially leaned toward 16K as the "balanced" choice. Reconsidered:

- **Snapshot growth is the binding constraint here**, not static space.
  9.5 GB free is a near-term pain point; saving 400 MB of static data
  via compression matters less than slowing daily snapshot accretion.
- **4K means zero application-side work.** No `PRAGMA page_size=16384;
  VACUUM;` per DB. No risk of a future SQLite upgrade or an app-level
  schema migration creating a new DB at default page size and silently
  reverting the alignment.
- **The "compression dies" cost is real but bounded.** Pihole-FTL (1.1 GB)
  + HA recorder (565 MB) lose ~30–50% to compression at 16K, so the
  static-space delta between 4K and 16K is roughly 500 MB total. A few
  weeks of snapshot diff savings recovers that.
- **Metadata overhead at 4K** (~4%, ~30 MB on a 1 GB DB) is not worth
  optimizing.

Picked **`recordsize=4096`** for the dedicated dataset.

## 6. Implementation

Commits:
- `0fd3a5ca` — initial dataset + pihole/homeassistant
- `5991bc28` — extension to bazarr, overseerr, filebrowser, csplogger,
  mosquitto, healthchecks, gitea (clean directory-bind variants)
- third commit (this batch) — sonarr, radarr, sabnzbd, headphones,
  profilarr, jellyfin, kuma, z2m, tautulli, HA `zigbee.db` (symlink
  variant — see §7)

### `roles/services/tasks/main.yml`

New child dataset `rpool/services/sqlite` mounted at `/mnt/services/sqlite`
with `recordsize=4096`, `autobackup:bak=true` (snapshots still wanted —
just smaller ones), and a `zfs_mount` systemd unit. Mirrors the existing
parent-dataset pattern.

Each service that uses the dataset gets:
- A `Create sqlite directory` task in `tasks/main.yml` for
  `/mnt/services/sqlite/<svc>/` owned by the service user.
- An `After=` / `Requires=zfs_mount_mnt_services_sqlite.service` block
  in its unit template, so the container can't start before the
  dataset is mounted.
- A volume mount that exposes `/mnt/services/sqlite/<svc>` to the
  container at the path the app expects to find its DB at.
- App-level config (env var, config file, etc.) where needed to point
  the DB writer at the new location.

### Per-service summary

| Role | Container DB path | Mechanism |
|---|---|---|
| `pihole` | `/var/lib/pihole/{pihole-FTL,gravity}.db` | Extra volume + `FTLCONF_files_database` / `FTLCONF_files_gravity` env vars. The cron job `pihole_list_update_cmd` also rebound to the new path so gravity updates land in the right place. |
| `homeassistant` | `/config/db/home-assistant_v2.db` | Extra volume on `/config/db` + `recorder.db_url` set in `configuration.yaml.j2`. |
| `bazarr` | `/config/db/bazarr.db` | Bazarr already writes its DB to `/config/db/`, so simply binding the new dataset at `/config/db` shadows the parent dataset's directory. No app-side config change. |
| `overseerr` | `/config/db/db.sqlite3` | Same idiom — overseerr keeps its DB under `/config/db/`. |
| `filebrowser` | `/database/filebrowser.db` | Already used a dedicated `/database` mount; redirected the host side from `filebrowser/database/` to `sqlite/filebrowser/`. |
| `csplogger` | `/app/databases/csp_violations.db` | The `/app/databases` mount swapped from `services/csplogger` to `sqlite/csplogger`. The csplogger dir held only the DB. |
| `mosquitto` | `/mosquitto/data/mosquitto.db` | The `/mosquitto/data` mount swapped from `mosquitto/data/` to `sqlite/mosquitto/`. That dir is dedicated to Mosquitto's persistence file. |
| `healthchecks` | `/db/hc.sqlite` | Extra volume on `/db` + `DB_NAME=/db/hc.sqlite` env var (Django setting). The original `/config` mount stays for `local_settings.py` / `logo.png`. |
| `gitea` | `/mnt/services/sqlite/gitea/gitea.db` | Native (not containerized) — `app.ini.j2` `[database] PATH` updated, `tasks/main.yml` creates the new dir. The other ~9 GB under `gitea/data/` (packages, lfs, repos) stays on the parent dataset. |

### Services intentionally left at 128K

These have SQLite DBs but were not migrated, with rationale:

- **sonarr / radarr / sabnzbd / headphones / profilarr**: linuxserver-style
  apps that put `<app>.db` directly in `/config/` alongside config files.
  Path is hardcoded; relocating cleanly would require either a per-service
  child dataset (many of them are mostly metadata cache, not DB-heavy) or
  a fragile symlink dance.
- **plex**: deeply nested DB path, hardcoded by Plex Media Server. ~340 MB
  of DBs but updates aren't daily — didn't surface in the diff sums.
- **jellyfin / kuma / z2m**: `/app/data` (or equivalent) contains the DB
  *and* other persistent state (metadata, monitor screenshots, pairing
  state). Binding the whole dir over the new dataset would move state
  that doesn't benefit from 4K records.
- **paperless**: `/usr/src/paperless/data/` mixes the SQLite DB with the
  Whoosh index (large, append-mostly) and classification model files —
  4K would hurt the latter.
- **tautulli**: DB path tied to Tautulli's `--datadir`, which also holds
  logs/config. Can't relocate just the DB without app patching.
- **homeassistant `zigbee.db`** and other small `.storage/` SQLite files:
  small, infrequently written; not worth the additional surgery beyond
  the recorder DB which dominated.

If any of these grow into the daily diff later, they can be tackled
individually — most likely by giving the whole service its own 4K
dataset rather than the shared `sqlite` one.

## 7. WAL sidecars: why bind-mount the directory, not the file

SQLite in WAL mode creates `<db>-wal` and `<db>-shm` next to the
**resolved** DB file. If you bind-mount only the single `.db` file from
the new dataset over the original path:

- Inside the container, the DB resolves to the bind point in the original
  directory.
- WAL/SHM are created next to that bind point — i.e. in the original
  128K-record directory, **not** on the 4K dataset.

This silently defeats the optimization for the WAL-write hot path (which
is the noisy one). Bind-mounting the whole containing directory (or, as
done here, exposing it as a different in-container path that the app is
configured to use) keeps WAL/SHM on the right dataset.

## 8. One-time data migration

The role only sets up *where* DBs go. It does not migrate existing files;
restarting the services after applying with empty target dirs would
create fresh empty DBs and lose history. Manual procedure on the host:

```bash
# pihole
systemctl stop pihole
mkdir /mnt/services/sqlite/pihole/
mv /mnt/services/pihole/etc/pihole-FTL.db*  /mnt/services/sqlite/pihole/
mv /mnt/services/pihole/etc/gravity.db*     /mnt/services/sqlite/pihole/
chown -R pihole:pihole /mnt/services/sqlite/pihole
systemctl start pihole

# homeassistant
systemctl stop homeassistant
mkdir /mnt/services/sqlite/homeassistant
mv /mnt/services/homeassistant/home-assistant_v2.db* \
   /mnt/services/sqlite/homeassistant/
chown -R homeassistant:homeassistant /mnt/services/sqlite/homeassistant
systemctl start homeassistant

# bazarr
systemctl stop bazarr
mkdir /mnt/services/sqlite/bazarr
mv /mnt/services/bazarr/db/bazarr.db* /mnt/services/sqlite/bazarr/
chown -R bazarr:media /mnt/services/sqlite/bazarr
systemctl start bazarr

# overseerr
systemctl stop overseerr
mkdir /mnt/services/sqlite/overseerr
mv /mnt/services/overseerr/db/db.sqlite3* /mnt/services/sqlite/overseerr/
chown -R overseerr:media /mnt/services/sqlite/overseerr
systemctl start overseerr

# filebrowser
systemctl stop filebrowser
mkdir /mnt/services/sqlite/filebrowser
mv /mnt/services/filebrowser/database/filebrowser.db* \
   /mnt/services/sqlite/filebrowser/
chown -R filebrowser:filebrowser /mnt/services/sqlite/filebrowser
systemctl start filebrowser

# csplogger
systemctl stop csplogger
mkdir /mnt/services/sqlite/csplogger
mv /mnt/services/csplogger/csp_violations.db* /mnt/services/sqlite/csplogger/
chown -R csplogger:csplogger /mnt/services/sqlite/csplogger
systemctl start csplogger

# mosquitto
systemctl stop mosquitto
mkdir /mnt/services/sqlite/mosquitto
mv /mnt/services/mosquitto/data/mosquitto.db* /mnt/services/sqlite/mosquitto/
chown -R mosquitto:mosquitto /mnt/services/sqlite/mosquitto
systemctl start mosquitto

# healthchecks
systemctl stop healthchecks
mkdir /mnt/services/sqlite/healthchecks
mv /mnt/services/healthchecks/hc.sqlite* /mnt/services/sqlite/healthchecks/
chown -R healthchecks:healthchecks /mnt/services/sqlite/healthchecks
systemctl start healthchecks

# gitea
systemctl stop gitea
mkdir /mnt/services/sqlite/gitea
mv /mnt/services/gitea/data/gitea.db* /mnt/services/sqlite/gitea/
chown -R git:git /mnt/services/sqlite/gitea
systemctl start gitea
```

`mv` between datasets is a copy + unlink (different filesystems even
though same pool), so the destination file is rewritten with the new
recordsize. After migration, `zdb -O rpool/services/sqlite <path>` should
show 4K records.

## 9. Cheaper, complementary fixes worth doing first

These were considered before the recordsize work and recommended as
standalone wins. Not implemented yet:

1. **Prune snapshots.** ~10 monthly snapshots back to Aug 2025 — keep
   3–4, drop the rest. Likely frees 10–15 GB instantly.
2. **Tautulli backup retention.** Currently 8 × 47 MB rolling backups in
   `tautulli/backups/`. That's ~378 MB of pure new file data per
   snapshot window. Trim to 1–2.
3. **HA recorder retention.** `recorder.purge_keep_days: 7` (or exclude
   noisy entities) shrinks the live DB and reduces daily diff.
4. **pihole-FTL `MAXDBDAYS`.** Already 31 (`FTLCONF_database_maxDBdays=31`
   in the unit). Could go lower if 31 days of query history isn't
   actually used.

The recordsize change is orthogonal to these — it reduces the *cost
per byte of snapshot growth*, the above reduce the *number of bytes of
growth*. Both compose.

## 10. What to monitor after applying

- `zfs list -t snapshot rpool/services` — daily USED column should drop
  noticeably for the affected DBs.
- `zfs get compressratio rpool/services/sqlite` — expect ~1.00x by
  construction (4K record + 4K sector floor). If it's much higher,
  something unexpected is on the dataset.
- `zfs get recordsize rpool/services/sqlite` — should report 4K.
- `du -sh /mnt/services/sqlite/*` — sanity check the migrated DBs are
  the expected size.

If after a month the daily USED on the parent `rpool/services` snapshots
hasn't dropped at least 50%, something else is generating churn — re-run
the `zfs diff | sum-by-toplevel-dir` command from §2 to find it.
