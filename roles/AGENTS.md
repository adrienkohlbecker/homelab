# Role Guidelines

Conventions for writing roles under `roles/`. The root [AGENTS.md](../AGENTS.md) still applies, Hard Rules first.

## Role conventions

**Load-bearing idioms** — these break silently if missed:

- Gate test-only branches on `qemu_test` (set by the test inventory's `test` group) only as a last resort, for what a fixture lacks (hardware, a live network peer, clock sync) — never to dodge a failing path — and say in a comment what goes untested.
- Per-role test hooks live alongside the role's tasks:
  - `tasks/_setup.yml` — pre-role fixture bringup. Never runs against prod.
  - `tasks/_verify.yml` — post-converge assertions, required in every role (lint: `require-role-verify`). Assert what the consuming binary or service observably does, never just that converge wrote what it was told; omit a check that can't. Only invoked by the harness against a qemu VM — never against prod. That contract lets scaffolding sit alongside real tasks without a `when: qemu_test` gate. Rebooting inside `_verify` is fine for next-boot state (canonical [roles/console/tasks/_verify.yml](console/tasks/_verify.yml)). Fixture VMs are ephemeral: no end-of-run teardown — only mid-run resets that a later check in the same file depends on.
- **Check mode:** every test cell runs `--check` on a fresh fixture first (see root *Testing*), and it predicts user, group, directory, and unit creation. `<svc>_user.uid`/`.gid` come from `static_service_ids`, so they are published even then, and local-source `copy`/`template` and `file: state=directory` pass against predicted paths and owners. Gate only tasks whose module needs the predicted state on the host (`remote_src` copies, `unarchive`, `authorized_key`) and start/restart on `not (ansible_check_mode and (...changed))` over the pending results, so a real run never silently skips. `ansible_check_mode` reflects only the CLI flag: under task-level `check_mode: true` in `_verify` these guards don't fire, so import only the task file under test, assert on its published results, and keep `check_mode: false` out of that file unless something else gates it.
- Prefer `import_role`/`import_tasks` over `include_*`. Fall back to `include_*` only for genuinely dynamic name/vars, then wrap the loop body in a per-iteration `include_tasks` for fresh `vars:` scope (`set_fact` still persists across iterations, so reset a conditional fact at the top). (lint, warning only: `prefer-import`)
- Static fixture playbooks under `test/playbooks/` must set `tasks_from` on every `import_role`. Their dependencies are named role entrypoints, never an implicit `tasks/main.yml`; the dynamic `site.yml` driver is the exception because it deliberately exercises the role under test through its normal entrypoint. (lint: `require-named-role-entrypoint`)
- Never arch-skip a whole role (`meta: end_play`, a top-level `ansible_architecture` gate); gate only the arch-specific tasks, so aarch64 cells still exercise the rest.
- Gate release features by excluding old releases (`!= 'noble'`, `not in ['jammy', 'noble']`), never `== '<current>'`.
- For state-mutating tasks that should run once, gate with `args: creates: <sentinel>` not `changed_when: false`.
- **Never branch a task `when:` on `inventory_hostname`** — a new host silently misses a hardcoded list. Set a `<svc>_enabled` flag in the host's inventory vars and read it as `<svc>_enabled | default(false)`. Play-level `hosts:` patterns in `site.yml` are the legitimate exception. (lint: `no-inventory-hostname-when`)

**Style:**

- Every task within a role carries the role name as a tag, even when the role invocation is already tagged, so task files stay independently reusable. `_`-prefixed test scaffolding, helper roles, and task files other roles call through `tasks_from` are exempt; their callers supply the tag scope. (lint: `require-role-tag`)
- Centralize every pinned upstream version — container image tags, downloadable artifacts (debs/tarballs/binaries), and package-manager pins — in [group_vars/all/versions.yml](../group_vars/all/versions.yml): full URL adjacent to its sha256, keyed by `ansible_architecture` (`x86_64`/`aarch64`) for multi-arch assets. Roles consume the pin by var name (ansible resolves vars globally); a role's `vars/main.yml` keeps only derived/computed values (e.g. arch-selected binary names, paths built from a version), never the raw pin.
- Every config-writing `copy:`/`template:` carries a best-effort `validate:` that parses the rendered file. Omit rather than invent a fake one. Safe as `validate:` args but don't lift into `command:`/`shell:` — embedded quotes break task loading. (lint, warning only: `require-validate`)
- Every file-writing task sets `backup: true` (lint: `require-backup`; test scaffolding and `_`-roles exempt; exceptions carry `# noqa: require-backup`). Keep it on secret-rendering templates too: backups inherit the source mode, are pruned after 7 days by [roles/cleanup](cleanup), and are excluded from the offsite replica.
- `/mnt/services/<svc>/` is service *state* (configs, DBs, secrets — rides ZFS snapshots). Service *code* from the repo belongs at `/opt/<svc>/`. Canonical: [roles/homepage/tasks/main.yml](homepage/tasks/main.yml).

## Adding a service

A new service role mirrors a recent sibling and wires these shared places:

- **Play:** `site.yml` is a layer ladder; add the role at the first layer whose guarantees it needs (placement rule in the file header), keeping dataset producers ahead of consumers.
- **Pin:** the image tag (or artifact URL and sha256) goes in `group_vars/all/versions.yml`. Prefer a multi-arch tag — prod is x86_64, local and `arm:` CI cells are aarch64 — and gate an amd64-only image and its probes on `ansible_architecture == 'x86_64'`.
- **Inventory:** required inputs are asserted by the role and set in both the prod inventory vars and the test ones (`group_vars/test.yml` or `test/host_vars/`), or the fixture's `assert:` fails. Gate per-host enablement with `<svc>_enabled` (see root *Inventory layout*).
- **Tests:** a functional `tasks/_verify.yml`, plus `meta/test.yml` when the role needs cells beyond the default Lab one (see root *Continuous Integration*).
- **Ports:** operator-reachable ports (host `--publish` or loopback binds) live in `service_ports:` in `group_vars/all/main.yml` — check it for collisions. Container-to-container traffic over podman networks stays as inline literals, and so does the container side of a `--publish` (or a role variable when two files must agree) — never a `service_ports` entry.
- **Datasets:** per-site gates are `zfs_has_<name>_mount:` (services/scratch/media/data/minio) in the same file. Consumers gate bind-mounts on the flag; producers create datasets unconditionally and never read it — test fixtures flip every flag `false`, so a gated producer would no-op there.
- **Vhost:** an `nginx_site` call (see *Helper roles*).
- **Bookmark:** user-facing services get a `homepage_bookmarks` entry under the right section in [roles/homepage/vars/main.yml](homepage/vars/main.yml), shaped `{ name: …, abbr: XX, sub: <subdomain>, icon: <selfh.st name> }` (add `host:` for another host's service). [bookmarks.yaml.j2](homepage/templates/bookmarks.yaml.j2) renders it to `icon: sh-<icon>.png` and `href: https://<sub>.<host>.{{ domain }}/`, and falls back to the abbr tile when `icon:` is omitted.

## Helper roles

Prefer these over re-implementing boilerplate. Call them with `tasks_from` and a single `*_args` dict, passing any `condition` inside it rather than as `when:` on the import. Entry points that publish no result (the `remove` ones) take no `condition`; gate those with `when:`. Authoring contract (empty `main.yml`, `condition`, removal entry points, tag scope) and full API: [notes/helper-roles-reference.md](../notes/helper-roles-reference.md).

| Helper | Entry point | Exposes / creates |
|--------|-------------|-------------------|
| `service_user` | `tasks_from: user` | `<svc>_user.uid`/`.gid`; `/mnt/services/<svc>` dir |
| `systemd_unit` | `tasks_from: {install,unit,remove}` | unit/drop-in install results; repository-owned unit lifecycle and removal |
| `systemd_timer` | `tasks_from: {install,remove}` | paired `.service`+`.timer` units |
| `apt_source` | `tasks_from: {configure,remove}` | deb822 source + scoped signing key + optional series pin; `apt_source_<name>_changed` |
| `nginx_site` | `import_role: name: nginx, tasks_from: site` | vhost with TLS/HSTS/CSP + optional Authelia |
| `boot` | `import_role: name: boot, tasks_from: {cmdline,initramfs_provider,initramfs_rebuild}` | kernel cmdline fragment; release image generator + policy; image rebuild with an optional reboot request (boot's `main.yml` is a real role entry point) |
| `zfs_dataset` | `tasks_from: {dataset,remove}` | ZFS filesystem + mount unit, or a plain mountpoint on non-ZFS hosts; retirement that keeps the dataset's snapshots |

## Podman Service Conventions

Rationale: [notes/podman_conventions.md](../notes/podman_conventions.md).

- **Images:** use canonical upstream names in service templates (`docker.io/sonatype/nexus3:3.91.1`, not `nexus.lab.fahm.fr/docker.io/…`); mirror redirection belongs in `registries.conf` + the `--upstream-mirrors` test flag.
- **Unit skeleton:** `lint:podman-service-templates` requires the shared notify/cidfile/journald skeleton (`Type=notify`, `--sdnotify=healthy`, `--cidfile`, `--log-driver journald`, …) in every `*.service.j2` — mirror a sibling unit rather than writing one from scratch.
- **Healthchecks:** every `*.service.j2` declares `--health-cmd` and `--health-startup-cmd` (lint: `podman-service-templates`), preferring in order:
  1. a service-native CLI — `mosquitto_sub -t ...`, `dig +short @127.0.0.1`;
  2. `curl`/`wget` already in the image — grep the Dockerfile first;
  3. Python `urllib.request` in JSON-array form, which survives the systemd→podman handoff: `--health-cmd '["python","-c","import urllib.request as u, sys; u.urlopen(sys.argv[1], timeout=1)","http://localhost:PORT/"]'`.

  A distroless image gets a bind-mounted static curl and a JSON-array `--health-cmd`, since it has no `/bin/sh`.
- **Secrets**, in order:
  1. app-native `*_FILE` — `--secret=<n>,type=mount,target=<basename>` + `--env XXX_FILE=/run/secrets/<basename>`; never lands in env;
  2. linuxserver `FILE__<VARNAME>` — s6-overlay reads the file at startup;
  3. `type=env,target=VAR` — last resort, visible to `podman inspect`.

  `*_FILE`/`FILE__` expansion reaches only the app's own processes, not `podman exec` or `--health-cmd`: read `/run/secrets/<basename>` there.
- **User namespacing**, in order:
  1. `--user {{ <svc>_user.uid }}:{{ <svc>_user.gid }}` (default; no mapping) whenever the app doesn't insist on `id -u == 0`;
  2. linuxserver `PUID`/`PGID` — s6-overlay aligns the baked-in `abc` user;
  3. fake-root uidmap — `--user 0:0` + `--uidmap=0:0:65536 --uidmap=+0:{{ <svc>_user.uid }}:1` (canonical [roles/jellyfin/](jellyfin/)). When the entrypoint drops to a baked-in non-root uid, allocate a dedicated host user and add a `+N:<uid>:1` override (canonical [roles/nexus/](nexus/)).
- **PID 1 / `--init`:** add `--init` (right after `--name`) only when the image runs the application directly as PID 1 — a bare Go/Java/.NET/node binary as `ENTRYPOINT`; podman's `catatonit` then reaps children and forwards `SIGTERM` (canonical [roles/adguard/](adguard/), [roles/jellyfin/](jellyfin/)). Skip it when the image ships its own init — s6-overlay `/init` (every `linuxserver/*`, home-assistant), `dumb-init`/`tini`, or an entrypoint that `exec`s a supervisor. Check, don't guess: `sudo podman image inspect <img> --format '{{json .Config.Entrypoint}}'`, and read the script if it's a shell wrapper.
- **systemd scope:** new timers and services are system-scope; reach for user-scope (linger) only when fundamentally required. `systemd-analyze security <unit>` lists cheap hardening wins.
- **Inter-container DNS:** containers reach co-located podman services via `<name>.dns.podman` (aardvark-dns), never a host port or hard-coded IP. Producer and consumer each create the `containers.podman.podman_network` (idempotent); it needs the netavark backend with `disable_dns: false`. Canonical: [roles/mosquitto/](mosquitto) (producer) + [roles/z2m/](z2m) (consumer). Target the network name or gateway IP, never an `ethN` index.
