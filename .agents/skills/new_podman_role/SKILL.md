---
name: new_podman_role
description: Scaffold a new podman-based service role following the homelab repo conventions. Use when adding a new self-hosted service that runs as a container — sets up service_user, podman_secret, systemd_unit helper invocations, healthcheck per the four-tier preference, nginx subdomain + homepage bookmark, and a `_verify.yml` exercising the live service.
---

# New podman service role

Walk the user through scaffolding a new role under `roles/<svc>/` that follows every convention documented in `AGENTS.md → Repo Conventions` and `AGENTS.md → Podman Service Conventions`. Each step references the section that authoritatively documents the rule.

## Inputs to collect from the user

Before scaffolding any file:

1. **Service name** (`<svc>`). Underscores, not hyphens (per *Coding Style & Naming Conventions*).
2. **Image reference** (full registry path + tag, e.g. `linuxserver/foo:1.2.3`). Note whether it's a `linuxserver/*` image — affects healthcheck and secret options.
3. **Loopback port** to publish on. Pick one not already in `group_vars/all/main.yml` `service_ports:` (per *Service Ports*).
4. **Subdomain** for nginx + homepage bookmark (default: `<svc>.{{ inventory_hostname }}.{{ domain }}`).
5. **Site bind-mounts needed** — any of `services`, `scratch`, `media`, `data`, `brumath`, `eckwersheim`, `minio`. Each is gated on `zfs_has_<name>_mount` (per *ZFS Site Mountpoints*).
6. **Secrets** the service needs. For each, pick a tier (per *Podman Service Conventions → Secrets*):
   - Tier 1: app-native `*_FILE` (preferred).
   - Tier 2: linuxserver `FILE__<VARNAME>` prefix (for linuxserver images only).
   - Tier 3: `type=env,target=VAR` (last resort).
7. **Healthcheck strategy** (per *Podman Service Conventions → Healthchecks*):
   - Tier 1: service-native CLI (`mosquitto_sub`, `dig +short @127.0.0.1`, etc.).
   - Tier 2: `curl` / `wget` in the image — grep the image's Dockerfile first.
   - Tier 3: python `urllib.request` (JSON-array argv form — see notes for the gotcha).

## Files to scaffold

For service `<svc>`, create:

- `roles/<svc>/tasks/main.yml` — orchestrates `service_user` → secret creation → unit install. Uses `import_role` calls into the helpers (per *Helper Roles*). Pattern:
  ```yaml
  - import_role:
      name: service_user
      tasks_from: user
    vars:
      service_user_args:
        name: <svc>
    tags:
      - <svc>

  - import_role:
      name: podman_secret
      tasks_from: secret
    vars:
      podman_secret_args:
        name: <svc>_<purpose>
        data: "{{ vault_<svc>_<purpose> }}"
    tags:
      - <svc>

  - import_role:
      name: systemd_unit
      tasks_from: install
    vars:
      systemd_unit_args:
        src: <svc>.service.j2
        condition: "{{ not (ansible_check_mode and <svc>_user.changed) }}"
    tags:
      - <svc>

  - import_role:
      name: systemd_unit
      tasks_from: service
    vars:
      systemd_unit_args:
        src: <svc>
        condition: "{{ not (ansible_check_mode and (<svc>_service_result.changed or <svc>_user.changed)) }}"
        restart: "{{ <svc>_service_result.changed or <svc>_<purpose>_rotated }}"
    tags:
      - <svc>
  ```
  Add `when: not (ansible_check_mode and <svc>_user.changed)` gate on tasks that depend on the freshly-created user/dir (per *Role Conventions*).
- `roles/<svc>/templates/<svc>.service.j2` — the systemd unit. Required pieces:
  - Container user (per *User Namespacing*, three-tier pick — simplest that works):
    - Default: `--user {{ <svc>_user.uid }}:{{ <svc>_user.group }}`. Works for any app that doesn't insist on `id -u == 0` inside.
    - linuxserver images: set `PUID` / `PGID` env vars instead.
    - Last resort: fake-root uidmap (`--user 0:0` + `--uidmap=0:0:65536 --uidmap=+0:{{ <svc>_user.uid }}:1` and gidmap) when the image actually needs in-container root. Add `+N:<uid>:1` for any image-side privilege drop you've observed.
  - `--health-cmd` per the tier you picked. Use JSON-array form for python/distroless.
  - `--publish 127.0.0.1:{{ service_ports.<svc> }}:<container_port>/tcp`.
  - Site bind-mounts gated on `zfs_has_<name>_mount` if any.
  - Secret injection per chosen tier.
- `roles/<svc>/tasks/_verify.yml` — functional checks (per *Role Conventions → _verify.yml*). At minimum:
  - Probe the published port (`uri:` to `http://localhost:{{ service_ports.<svc> }}/`).
  - Hit the nginx vhost via `Host` header: `uri:` to `https://localhost/`, `headers: { Host: <subdomain>.<host>.<domain> }`, `validate_certs: false`.
  - Ownership assertions on bind-mount dirs (per *User Namespacing*) — `stat:` + `failed_when: result.stat.pw_name != '<svc>'`.
  - Stat-only checks ("unit active", "file present") drift toward tautology — exercise the real behavior wherever feasible.

## Cross-role registration

After the role itself is scaffolded:

1. Add the port to `group_vars/all/main.yml`'s `service_ports:` dict.
2. Add an nginx subdomain — usually a `nginx_site_args:` in the role's `main.yml` proxying to `http://localhost:{{ service_ports.<svc> }}/`.
3. Add a bookmark in `roles/homepage/templates/bookmarks.yaml.j2` (per *Homepage Bookmarks*):
   ```yaml
   - <Display name>:
       abbr: XX
       icon: sh-<svc>.png
       href: https://<subdomain>.{{ inventory_hostname }}.{{ domain }}/
   ```
   Pick the right section (Media / Infra / Personal). Drop the `icon:` line if there's no selfh.st icon (the `abbr:` becomes the fallback).
4. Wire the role into `site.yml` for the target host group.

## Test

Run `test/testrole.py <svc>` (boots `box` by default). Confirms converge + idempotence + `_verify.yml`. For producer roles needing real multi-pool layout, `--machine lab` or `--machine pug`.

## Things this skill will NOT do

- Choose ports / subdomains / secret names for the user. Ask.
- Skip the `_verify.yml`. Every new role gets one.
- Use `defaults/main.yml` (per *Hard Rules*). Inputs come from `host_vars`/`group_vars`.
- Use Ansible handlers for restarts (per *Hard Rules*). Use the `systemd_unit` helper's inline OR chain.
