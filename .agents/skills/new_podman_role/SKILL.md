---
name: new_podman_role
description: Scaffold a podman-backed homelab service role with the repo's helper roles, healthcheck, nginx site, homepage bookmark, and qemu verification.
---

# New Podman Service Role

Scaffold one container-backed service role. `AGENTS.md` is the source of truth;
mirror nearby roles before inventing structure.

## Required Facts

Collect or infer these before writing files:

- Service name: underscores, not hyphens.
- Canonical image reference and tag. If pinned, put the version/URL/hash in
  `group_vars/all/versions.yml`, not role vars.
- Published loopback port. Check both `service_ports:` and existing
  `127.0.0.1:` literals before allocating.
- Nginx subdomain and homepage label/icon.
- Required site bind mounts: `services`, `scratch`, `media`, `data`,
  `brumath`, `eckwersheim`, or `minio`.
- Secrets and injection mode.
- Healthcheck command, verified against the image.

Ask only for facts that are real domain choices or cannot be proven from the
repo. Do not invent secret boundaries.

## Files

- `roles/<svc>/tasks/main.yml`: assert required inputs, create the service user,
  create podman secrets, install config with `backup: true`, register the nginx
  site, install the unit, then start/restart it through `systemd_unit`. Do not
  use handlers.
- `roles/<svc>/templates/<svc>.service.j2`: system-scope podman unit with the
  canonical image name, explicit user mapping, published loopback port,
  bind-mount gates, secrets, and `--health-cmd` plus `--health-startup-cmd`.
- `roles/<svc>/tasks/_verify.yml`: functional qemu checks. Probe the published
  port, the nginx vhost, health, and ownership of writable bind mounts.
- Cross-role wiring: `service_ports`, `site.yml`, and
  `roles/homepage/templates/bookmarks.yaml.j2`.

Required service inputs belong in `host_vars` or `group_vars` plus an `assert`.
Optional host-overridable values may live in `defaults/main.yml`.

## Podman Choices

- User: default to `--user {{ <svc>_user.uid }}:{{ <svc>_user.group }}`.
  Use linuxserver `PUID`/`PGID` for linuxserver images; use fake-root uidmaps
  only when the image truly requires in-container root.
- Secrets: prefer app-native `*_FILE`, then linuxserver `FILE__<VAR>`, then
  `type=env,target=VAR`.
- Healthcheck: prefer service-native CLI, then existing `curl`/`wget`, then
  Python `urllib.request` in JSON-array argv form.
- `--init`: add only when the image runs the app directly as PID 1. Skip images
  that already use s6, tini, dumb-init, or an entrypoint that `exec`s a
  supervisor.
- Networking: co-located containers should use `<name>.dns.podman`, not host
  ports or hard-coded IPs.

## Verification

Run the narrow role test first:

```sh
test/testrole.py <svc>
```

Use `--machine lab` or `--machine pug` only when the role needs a producer-side
layout that the default `box` fixture cannot model.

Before finishing, run the relevant lint target and inspect the diff for
unnecessary role vars, pass-through inputs, missing backups, missing
healthchecks, and stat-only verification.
