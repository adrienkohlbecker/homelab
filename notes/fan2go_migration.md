# fan2go migration runbook

Replacing the `fancontrol` role with `fan2go` on lab and pug. Both
roles live in site.yml during the transition; cutover is per-host via
host_vars + the per-host yaml file under `roles/fan2go/files/`.

## Shape

The fan2go role uses lightweight templating: per-host yamls live under
`roles/fan2go/templates/<inventory_hostname>.yaml.j2` and substitute
only `api.port` from `group_vars/all.yml:service_ports.fan2go_api`. The
rest is literal YAML — no curve or fan generation logic. The role-level
knob is a single bool: `fan2go_enabled` (default `false`). lab.yaml.j2
and pug.yaml.j2 are already present, ported from the old
`fancontrol_settings` dicts.

The role also installs `fan2go-tui` (interactive terminal UI) plus a
shared `fan2go-tui.yaml.j2` that pins the tui's api.port to the same
registry value. The tui connects to fan2go's HTTP API on loopback.
Run interactively: `fan2go-tui` on the host (no root required, no
service unit — foreground tool).

Both binaries build from source on every enabled host (fan2go-tui ships
no upstream binaries on any arch); the role installs a pinned Go
toolchain to `/opt/go-<version>/` for the builds.

## Per-host cutover (lab, pug)

In one host_vars edit per host:

1. Empty `fancontrol_settings.fans: []` (drives the fancontrol role into
   its stop+disable+remove branch).
2. Set `fan2go_enabled: true`.

Then `mise run ansible --limit <host> --tags fancontrol,fan2go`. Order
within the play matters: fancontrol stops first (site.yml lists it
before fan2go), then fan2go installs and starts. Avoids a brief window
where two daemons fight over the same PWMs.

## Verification

- `systemctl status fan2go` — active, no warnings.
- `fan2go curve` — prints live sensor → curve mapping; sanity-check
  values against mintemp/maxtemp in the original fancontrol_settings.
- `sudo fan2go-tui` — interactive view of live fan / sensor / curve
  state; sanity-check via the UI in addition to the CLI commands.
- `cat /sys/class/hwmon/hwmon*/fan*_input` — fans actually spinning.
- `journalctl -u fan2go -b` — silent. Any "pwmValueChangedByThirdParty"
  warning means fancontrol is still active; `systemctl status fancontrol`.

## After both hosts are migrated

1. Drop `fancontrol` from site.yml (the line above `fan2go`).
2. Delete `roles/fancontrol/` directory.
3. Remove `fancontrol_settings` from host_vars/lab.yml + pug.yml.
4. Trim the fancontrol/fan2go comment in host_vars/box.yml.

## Open items deferred to follow-up

- **drivetemp by-id, not first-match**: fan2go's `platform: drivetemp`
  matches the first ATA device the kernel registers — race-y across
  reboots when multiple disks are present. The fan2go-correct fix is
  `sensor.disk.device: /dev/disk/by-id/<wwn>`. Inherits a latent bug
  from fancontrol; not worth blocking the cutover on.
- **fan2go API + Prometheus**: both disabled by default in the
  hand-written yaml. fan2go-prometheus on port 9000 would let netdata's
  prometheus collector scrape fan curves directly. Enable once the
  basic migration is stable.
- **arm64 binary on nexus**: build-from-source on aarch64 (~150MB apt
  deps + Go tarball + build time) is heavy. Once fan2go is settled and
  upstream still doesn't ship arm64 binaries, build one in CI and
  host on `nexus.lab.fahm.fr/binaries/fan2go-linux-arm64-<version>`;
  switch the aarch64 branch in roles/fan2go/tasks/main.yml to a
  get_url against nexus.
