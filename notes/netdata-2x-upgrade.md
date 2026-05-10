# Netdata 1.46 → 2.x upgrade

## Why this is open

`roles/netdata/files/apt_preferences` pins `netdata*` to `1.46.*`. The pin
was added on 2024-09-21 (commit 7c54b4ee, "Pin netdata version") right
before netdata 2.0 dropped in October 2024 — likely to defer the major
upgrade. Current upstream stable is **v2.10.3** (April 2026), so we are
18 months and one major behind.

The pin is what made `roles/netdata/tasks/main.yml`'s "Disable pihole
collector in go.d.conf" workaround necessary: the 1.46.x bundled
go.d/pihole module hits `pi.hole/admin/api.php`, pi-hole v6 retired
that path, and FTL logs a `bad_request` warning every probe interval.
The 2.x module talks to `/api/auth` + `/api/stats/summary` and accepts
a `password:` config field — see the v6-aware config snippet at the
bottom of this note.

## What changes between 1.46 and 2.x

Rough survey of breaking-or-noisy items to check before bumping the pin;
not exhaustive. Read the upstream changelog before committing.

- **go.d collector relocation.** Modules moved from
  `src/go/collectors/go.d.plugin/modules/<x>` to
  `src/go/plugin/go.d/collector/<x>`. The on-disk config paths under
  `/etc/netdata/go.d/*.conf` did not change, but a few module configs
  added/removed fields. Sweep every `roles/netdata/templates/*.conf.j2`
  and every `roles/<svc>/tasks/netdata.yml` against the v2.10
  `config_schema.json`. Known sites:
    - `prometheus.conf.j2`
    - `intelgpu.conf`, `docker.conf`, `systemdunits.conf`
    - `roles/fail2ban/tasks/netdata.yml`, `smart`, `lm_sensors`,
      `certbot`, `nginx`
- **pihole collector** is v6-only and requires `password`. Remove the
  `pihole: no` override added in [roles/netdata/tasks/main.yml](../roles/netdata/tasks/main.yml)
  (and the `netdata_godconf_*` entries on the `Enable netdata service`
  restart chain) and replace with a `pihole.conf.j2` like:
  ```yaml
  jobs:
    - name: pihole
      url: http://127.0.0.1:8943
      password: {{ pihole_password | to_json }}
      autodetection_retry: 60
  ```
  Wired in via the existing `tasks_from: template` helper with
  `mode 0600`. Bind to the loopback publish in
  [roles/pihole/templates/pihole.service.j2](../roles/pihole/templates/pihole.service.j2)
  (currently port `8943`).
- **Default storage tier count** and **DB engine memory accounting**
  shifted between 1.x and 2.x. The role already pins
  `db.storage_tiers=2` so this is probably a no-op, but verify ML/eBPF
  disables still apply (the section names did not change in 2.x AFAICT).
- **Cloud / claim flow** changed. `cloud.conf` is still honored;
  `claim.conf` was added for newer claim tokens. We don't claim here so
  this should be inert, but confirm.
- **Health/alert engine.** Alert config syntax saw additions (notably
  `lookup: ... after: ... at-least: ...` chained forms). Existing
  `systemdunits_alerts.conf` + `zfs_alerts_override.conf.j2` should
  still parse; `netdatacli reload-health` will surface failures.
- **Plugin packaging.** 2.x split more plugins into separate
  `netdata-plugin-*` debs. The role already installs
  `netdata-plugin-slabinfo`; check the v2.10 package list for any new
  plugin we depend on (e.g. `netdata-plugin-go.d` if that gets split).

## Sequencing

1. Inventory each `roles/*/tasks/netdata.yml` consumer (fail2ban, smart,
   lm_sensors, certbot, nginx) and their corresponding `.conf` files.
   Diff each against the v2.10 stock conf.
2. Bump `roles/netdata/files/apt_preferences` to `2.10.*` (or `2.*` if
   you want to track minor bumps).
3. Re-add the v6 pihole config template + task; remove the `pihole: no`
   override.
4. Run the harness end-to-end on box, lab, pug variants
   (`test/testrole.py netdata --machine <variant>` for each).
   `_verify.yml` (if added) should curl the local nginx and check the
   netdata API responds; at minimum walk the journal for collector
   init failures.
5. Apply to lab first (single node), watch metrics + alerts for a
   couple of days, then roll to the rest.

## Open question

The 2.x line removed `python.d.plugin` in favor of `go.d`. Confirm none
of our integrations were on a python collector — a quick `grep -rn
python.d roles/netdata/` should turn up empty before the bump.
