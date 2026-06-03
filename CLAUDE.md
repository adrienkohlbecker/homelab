# Repository Guidelines

## Spirit & Trade-offs

This is a home environment, not a corporate production site. Tie-breakers when the Hard Rules don't decide a question:

- **Maintainability beats ambition.** The operator is also the on-call — an elegant rewrite they can't debug at 11pm is a regression. Default to simple-and-boring.
- **Security is pragmatic.** Reasonable hygiene (vault, firewall, file permissions) is in scope; defense against nation-state actors is not. New security machinery needs a concrete threat behind it.
- **Wife-acceptance-factor is real.** Household-depended services (home-assistant, z2m, media, dns) have higher cost-of-failure than operator-only infra. Visible breakage outweighs elegance.

## Hard Rules — DO NOT

Load-bearing negatives, up-front so a fresh session sees them first.

- **DO NOT use Ansible handlers for service restarts.** Handlers run at end-of-play, which doesn't compose with the `systemd_unit` helper's pre-pull-then-start ordering. Drive restarts via the helper's inline OR chain on `*_result.changed` / `*_rotated`. See *Helper roles → systemd_unit*.
- **DO NOT drop a container's `--health-cmd`** in favour of external monitoring (kuma, `_verify.yml`). Without an in-container check, `--sdnotify=healthy` can't gate the unit's `active` state and podman won't auto-restart on quiet HTTP failure. See *Healthchecks*.
- **DO NOT default required service inputs in `vars/main.yml`** — role vars sit *above* host_vars in ansible's precedence ladder and silently mask host-level overrides. Required inputs live in `host_vars`/`group_vars` and the role must `assert:` they're set. `defaults/main.yml` is fine for optional host-overridable values (`fan2go_enabled: false`, `certbot_extra_sans: []`) since it sits *below* host_vars. Canonical comment: [roles/github_runner/defaults/main.yml](roles/github_runner/defaults/main.yml).
- **DO NOT run state-mutating commands on prod hosts (`lab`/`pug`/`bunk`) without explicit ack.** Diagnostic SSH is pre-authorized; mutations are not. See *Debugging prod hosts directly*.

## Project Structure & Module Organization

`site.yml` orchestrates host groups → roles. `ansible.cfg` binds the repo to `hosts.ini` and `vault-client.sh` (dispatches on vault-id; see *Vault ids*), so running from the root picks up the correct inventory and vault passwords automatically.

| Path             | Purpose                                                                                 |
|------------------|-----------------------------------------------------------------------------------------|
| `roles/<r>/`     | role logic — `tasks/`, `templates/`, `files/`, `vars/` (no `handlers/`, per Hard Rules) |
| `group_vars/`    | shared defaults — directory split (`group_vars/all/main.yml` etc.)                      |
| `host_vars/`     | host-specific overrides                                                                  |
| `terraform/`     | DNS (Cloudflare), registrar (Gandi), Cloudflare Access, Mailgun, GitHub-repo, Nexus, GCP|
| `packer/`        | QEMU image builds                                                                       |
| `test/`          | harness, inventories, logs                                                              |
| `wireguard/`     | VPN peers (vaulted keys)                                                                 |
| `notes/`         | long-form design notes — landing zone for content that doesn't belong inline            |

## Build, Test, and Development Commands

- Bootstrap: install [mise](https://mise.jdx.dev), `mise trust`, then `mise install`. `python.uv_venv_auto` auto-sources `.venv`; `uv sync` populates Python deps. 1Password CLI must be signed in for the `op://` env vars in `mise.toml` to resolve.
- **op:// env refs only resolve under `op run --`.** Toml tasks wrap explicitly (`run = 'op run -- tofu "$@"'`); file-based tasks under `mise-tasks/` do **not** — mise exports the literal `op://…` string. Fix: re-exec under `op run --` behind a guard env var (preamble at top of [mise-tasks/ha/sync.py](mise-tasks/ha/sync.py)). Diagnostic: log `len=${#FOO_TOKEN}` — if ~40 (the `op://` string length), resolution didn't happen.
- Configure everything: `mise run ansible --limit prod` (wrapper handles vault-id, ssh args, env; set `--tags` to narrow scope). One service/host: `mise run ansible --limit lab --tags wireguard`.
- DNS/terraform: `mise run tf {init,plan,apply}` — `cd`s into `terraform/` and forwards to `tofu` (use `--` for flags mise intercepts). State in MinIO (`s3://terraform/homelab.tfstate`), AES-GCM-encrypted from `TF_VAR_state_passphrase`. Rotation: [notes/terraform-state-encryption-rotation.md](notes/terraform-state-encryption-rotation.md).
- Refresh integration image: `mise run packer:build [box|pug|lab]` (parallel; `--ubuntu noble` for another release). See [notes/test_environment_design.md](notes/test_environment_design.md).
- Lint: `mise run lint` (ansible-lint, tofu/packer fmt+validate, tflint, black/ruff, yamllint, shellcheck+shfmt, stylua+selene for the fluent-bit Lua filters — std/config in `lint/`, topology schema, all parallel); `mise run fmt` applies fixes (`fmt:ansible` = `ansible-lint --fix` — prefer over hand-editing). Inner-loop: prefer `mise run lint:ansible-changed` (~4s; changed YAML vs `origin/master` + uncommitted + untracked, override base via `LINT_BASE=<ref>`) over full `lint:ansible` (~40s). Run full `mise run lint` before pushing.
- Resume mid-converge: after a `testrole.py --keep` failure, `mise run ansible --limit <host> --start-at-task '<failing task name>'` rather than from scratch. `--step` walks task-by-task when bisecting.

## Workflows — use the skill, don't reinvent

- `/triage <service>` — investigate a service end-to-end (resolve host(s), gather state, summarize).
- `/split_worktree_commits` — split a multi-finding worktree into per-finding commits.
- `/new_podman_role` — scaffold a new podman service role per *Podman Service Conventions*.

## Coding Style & Naming Conventions

**Underscores, not hyphens** in identifiers we author: role names, files under `roles/`, systemd units, vars, dirs under `/mnt/services/<svc>/` — `clickhouse_logs` not `clickhouse-logs`. Exceptions are names dictated by upstream (image tags, package names, K8s-flavored YAML keys). Everything else (YAML indentation, descriptive `name:`, namespaced role vars) is enforced by `mise run lint`.

**Never set `no_log: true`.** It hides the rendered diff that makes a failed converge debuggable, and its only upside buys nothing here: applies run interactively on the operator's workstation, never captured to a file or CI log. This holds even for tasks that render a vaulted value (`template:` of a secret config, a `--dump` printing a password): let it show.

**Every bash script starts `set -euo pipefail`** — proper files *and* inline ansible `shell:` blocks, which must also declare `executable: /bin/bash` (`pipefail` is a bashism; default `/bin/sh` is dash). Enforced for `shell:` tasks by the custom **`shell-strict-mode`** rule ([lint/ansible_rules/shell_strict_mode.py](lint/ansible_rules/shell_strict_mode.py)). Test scaffolding (`_verify*`/`_setup*`) is **exempt**. `-e` obliges you to handle expected-failure commands: `|| true` where non-zero is normal (`snap list` exiting 1 when empty), and avoid `… | head` pipelines where the early reader raises SIGPIPE — collapse into one non-early-exiting `awk`. Canonical: snap-purge in [roles/cleanup/tasks/main.yml](roles/cleanup/tasks/main.yml).

**Comments describe current state, not history.** No "this used to…", "was removed", "replaces the old…", or "NOTE: X lived here". That context belongs in the commit message / PR — tied to the change, where it won't rot or mislead someone reading the code as it is now. A comment that only makes sense to a reader who remembers the prior version is dead weight; delete it. (Explaining why the current code *deliberately* avoids an obvious-looking alternative — "not X because Y" — is current state and fine.)

## Repo Conventions

### Role layering (`site.yml`)

`site.yml` is ordered as a **layer ladder** — a role's converge position *is* its layer, and each layer builds on the guarantees of the ones above. Two bands:

- **Base machine install** (`hosts: box,lab,pug,fox`) — runs on every managed host. Sub-bands top-to-bottom: *host base* (OS, networking, access, system services), *storage & boot* (`zfs`/`zfs_mount`/`zfs_autobackup`/`zfsbootmenu`/`refind`), *service platform* (`podman`, `services`, `certbot`, `nginx`), *observability* (`csplogger`/`netdata`/`fluentbit`). Roles within the host-base band are mutually independent — order there is immaterial.
- **Services** — host-scoped plays (`External VPS (fox)`, `Lab and Pug`, `Lab roles`) that assume the full platform above is already in place.

**Where does a new role go?** Walk down the ladder, stop at the first layer whose guarantees you need:

- needs only a booted OS → host base.
- needs ZFS datasets/snapshots → after the storage band.
- needs podman / `/mnt/services` / nginx-TLS → after the platform band (this is the base⇄service watershed).
- it's an app only a subset of hosts run → a service play, scoped by host capability.

Everything in the base bands runs on *every* ZFS-root host (fox included); everything below is host-scoped. Keep dataset *producers* (`scratch`/`data`/`media`, `services`) ahead of their consumers so converge order reads dependency-before-consumer.

### Role conventions

**Load-bearing idioms** — these break silently if missed:

- Gate test-only branches on `qemu_test`: set in test-fixture host_vars (`host_vars/{box,minimal}.yml`) and in `host_vars/{lab,pug}-qemu.yml` overlays the harness layers on when `--machine lab/pug`. Use it in `when:`/`ternary`, not per-machine name comparisons. `minimal` also sets `qemu_test_minimal: true`.
- Per-role test hooks live alongside the role's tasks:
  - `tasks/_setup.yml` — pre-role fixture bringup. **Never runs against prod.**
  - `tasks/_verify.yml` — post-converge assertions hitting the service over its port / triggering the failure path. **Only invoked by `test/testrole.py` / `test/testall.py` against a qemu VM** — never by `site.yml` or `--tags <role>` against prod. That contract lets scaffolding (netns/veth, sentinel tables, test listeners) sit alongside real tasks without a `when: qemu_test` gate. Stat-only checks drift toward tautology — fall back only when functional testing is impossible, and say why. Rebooting inside `_verify` is fine when the role writes next-boot state — canonical [roles/console/tasks/_verify.yml](roles/console/tasks/_verify.yml).
- Tasks depending on a freshly-created user/group/dir must be gated `when: not (ansible_check_mode and <svc>_user.changed)` — in check mode the prerequisite only *reports* creation, so a downstream `file:`/`copy:`/`template:` fails with "no such user/dir".
- Prefer `import_role`/`import_tasks` over `include_*`. Static forms parse-check at lint time, dedupe, and avoid the `loop:` + task-level `vars:` "vars bind once" bug. Fall back to `include_*` only for genuinely dynamic name/vars, then wrap the loop body in a per-iteration `include_tasks` for fresh scope.
- For state-mutating tasks that should run once (downloads, registrations, token fetches), gate with `args: creates: <sentinel>` not `changed_when: false` — skipped tasks don't count against the idempotence check, and `changed_when: false` lies about what the task did.

**Style:**

- Centralize downloadable artifact metadata in `roles/<role>/vars/main.yml`: full URL **adjacent to its sha256**, keyed by `ansible_architecture` (`x86_64`/`aarch64`).
- Bare module names (`command:`, `apt:`, `assert:`) for `ansible.builtin.*`; FQCN only for other collections (`containers.podman.podman_network`). Don't mix styles within a file.
- Every config-writing `copy:`/`template:` carries a best-effort `validate:` that *parses* the rendered file (catches jinja/vault typos — many services accept-then-overwrite-with-defaults on parse failure). Parser by format: `python3 -c 'import yaml,sys; yaml.safe_load(open(sys.argv[1]))' "%s"`, configparser for INI, `nginx -t -c %s`, `visudo -cf %s`. Safe as `validate:` args (passed to `%s`, not shell) but **don't** lift them into `command:`/`shell:` — the embedded single quotes break task loading. Omit rather than invent a fake one.
- **Preserve upstream comments in vendored config files.** When a role ships a service's stock config (a `files/*.conf` or `templates/*.conf.j2` derived from the package's annotated example), keep the upstream comment blocks rather than stripping to the load-bearing lines — they document each directive's meaning + default inline, which aids discoverability and 11pm troubleshooting far more than the lines cost. Deliberate exception to "comments describe current state": the upstream reference *is* current state for a config file. Author-added rationale still follows the usual rules. Canonical: [roles/nut_server/templates/upsd.conf.j2](roles/nut_server/templates/upsd.conf.j2).
- **Don't pin a config value just because it equals the upstream default** — that's churn. Pin (+ comment) only when *load-bearing*: it documents a dependency (headscale's `write_ahead_log: true`, required because the DB is on a 4K-recordsize dataset), is a deliberate non-default, or is a tuning knob worth surfacing.
- Every file-writing task (`copy:`/`template:`/`replace:`/`lineinfile:`/`blockinfile:`/`ini_file:`/`assemble:`) sets **`backup: true`** for on-disk traceability. Enforced by the custom **`require-backup`** rule ([lint/ansible_rules/require_backup.py](lint/ansible_rules/require_backup.py)); test scaffolding and `_`-roles exempt. No credential-exposure concern (`preserved_copy` keeps exact mode+ownership) and no bloat (cleanup prunes backups >1wk). Genuine exceptions carry `# noqa: require-backup`: binary/asset installs, `/run` transients, restore steps, secret-staging files, the vfat ESP.
- `/mnt/services/<svc>/` is service *state* (configs, DBs, secrets — must ride ZFS snapshots). Service *code* from the repo (`roles/*/files/*.py`, scripts) belongs at `/opt/<svc>/` — reproducible, no snapshot needed. Canonical split: [roles/homepage/tasks/main.yml](roles/homepage/tasks/main.yml).

### Service ports

Ports each host-level service publishes live in `group_vars/all/main.yml` under `service_ports:` — single source of truth for unit templates, nginx `proxy_pass`, firewall rules, cross-role consumers. **Scope: only operator-reachable ports belong here** — a host-published `--publish` or a loopback bind the operator hits via `localhost`. A port that exists only for container-to-container traffic over a podman network (e.g. mosquitto's 1883, reached at `mosquitto.dns.podman`) is not operator-facing; leave it an inline literal rather than registering it. Migrate roles to consume from there as you touch them ([notes/SOMEDAY.md](notes/SOMEDAY.md)). **When allocating a new port, grep both `service_ports:` and `127.0.0.1:` literals in `roles/*/templates/*.j2`** — un-migrated roles still hard-code their port.

### ZFS site mountpoints

Per-site dataset gates live in `group_vars/all/main.yml` under `zfs_has_<name>_mount:` (services/scratch/media/data/brumath/eckwersheim/minio). **Producers** (`roles/data`, `media`, `scratch`, `minio`, `services`, `libvirt`) create the dataset unconditionally on every `zfs_root` host and **never read the flag**. **Consumers** (unit templates, samba mount overrides) gate on `zfs_has_<name>_mount` to decide whether to bind-mount `/mnt/<name>`. Test fixtures flip every flag `false`, so qemu VMs exercise consumers without prod-data deps. Adding a mount: register the flag (default `true`), add a producer task, gate `BindReadOnlyPaths=/mnt/<name>` in every consuming `.service.j2`. Don't gate the producer — it would no-op under the default test machine.

### Homepage bookmarks

Any new user-facing service (nginx subdomain) gets a bookmark in [roles/homepage/templates/bookmarks.yaml.j2](roles/homepage/templates/bookmarks.yaml.j2) — pick the section (Media/Infra/Personal), follow the `abbr: XX` + `icon: sh-<name>.png` + `href: https://<subdomain>.{{ inventory_hostname }}.{{ domain }}/` shape. Icons resolve through selfh.st (`sh-` prefix); omit `icon:` if none exists and the 2-letter `abbr:` is the fallback.

### Home Assistant GUI YAML sync

All user-authored HA config under `/mnt/services/homeassistant/` (`automations/scripts/scenes.yaml`, `sensors/templates/input_numbers/timers.yaml`, `custom_templates/macros.jinja`, `blueprints/automation/*.yaml`) lives in a separate private repo ([adrienkohlbecker/homelab_ha_config](https://github.com/adrienkohlbecker/homelab_ha_config)) registered as a submodule at [roles/homeassistant/files/ha_gui_config/](roles/homeassistant/files/ha_gui_config) — the source of truth. The role only creates dirs + touches include-targets so HA's loader doesn't error on a fresh host (a missing stub sends HA into recovery mode, discarding configuration.yaml).

Drive both directions with `mise run ha:sync [pull|push|sync]` ([mise-tasks/ha/sync.py](mise-tasks/ha/sync.py)). Preview with `… push --dry-run`. A movable tag `last_synced_to_host` pins the commit matching lab; push refuses if the host's blobs diverge (host edited since pull → pull first). Each tracked file declares its reload service in `SYNC_SPEC`; files with no per-domain reload (currently `sensors.yaml`) trigger a full `homeassistant.restart`. Auth via `HA_API_TOKEN` (1P-backed `[env]` in [mise.toml](mise.toml)); without it the YAML lands on disk with a warning, reload manually. Every push parse-validates each changed file and aborts on syntax error.

Don't bypass the sync (no `scp`, no live-VM editing) — that strands the repo. Orphan paths are gitignored at the root. **Don't bump the parent's submodule pointer**: [.gitmodules](.gitmodules) sets `ignore = all`, the pinned commit is **not load-bearing** (deployment reads the submodule's working tree / `origin`, never the parent SHA), so a bump is pure churn and `git status` won't surface it. If ever needed deliberately: `git add --force roles/homeassistant/files/ha_gui_config`.

### `notes/` submodule

`notes/` is a **git submodule** → private repo [adrienkohlbecker/homelab_notes](https://github.com/adrienkohlbecker/homelab_notes), split out so long-form (often PII-adjacent) notes stay out of this **public** parent. The parent stores only a gitlink. The `git subtree split` that created it was one-time history surgery — this is **not** a subtree. **Never run `git subtree add/pull/push/merge`** against `notes/`.

Sync is the standard two-step submodule dance:
1. Edit inside `notes/`: `cd notes && git commit … && git push` → `homelab_notes`.
2. Record in parent: `git add notes && git commit` (bundle into a related parent commit, or a lone `notes: bump pointer`).

On a fresh clone, `git submodule update --init` checks out the recorded SHA; `--remote notes` advances to latest `homelab_notes/main` (then `git add notes` + commit). **Unlike `ha_gui_config`, the `notes` pointer IS load-bearing and carries no `ignore = all`** — divergence and a dirty `notes/` worktree *do* surface in parent `git status`, and you *should* deliberately commit pointer bumps. The SHA + timestamp land in the public repo; notes *content* never does. Since `homelab_notes` is private, `--init` only succeeds with SSH access (a stranger cloning the public parent gets an empty `notes/` — intended).

**In a worktree this is automated by the `worktree:` tooling** ([mise-tasks/worktree/](mise-tasks/worktree/), driven by `WorktreeCreate`/`WorktreeRemove` hooks in [.claude/settings.json](.claude/settings.json), firing for agent `isolation: worktree` too). `worktree:populate` inits `notes` on a branch matching the parent worktree; `worktree:merge` stays linear when safe (code-only or FF-able notes → rebase + FF, no SHA change) and `--no-ff` merges only when `notes/main` has diverged. Same-file conflicts halt for manual resolution. Each worktree gets its own submodule clone — notes reach the main checkout only via `origin`. A detached worktree (`new==base`) has no parent branch, so notes stays detached and the merge skips it.

### Helper roles

Prefer these over re-implementing boilerplate; a hand-rolled equivalent is a migration candidate. All take inputs through a single `*_args` dict (so each call replaces it wholesale and inter-call vars-leak can't happen).

- **`service_user`** — `tasks_from: user`, `service_user_args: { name: <svc> }` → standard system group + nologin user (`/nonexistent` home) + `/mnt/services/<svc>` config dir; exposes `<svc>_user.uid`/`.group`. Optional keys: `extra_groups`, `createhome`, `home`, `shell`, `config_dir: false`.
- **`podman_secret`** — `tasks_from: secret`, `podman_secret_args: { name, data }` creates/rotates a podman secret (the module byte-compares the stored value via `secret inspect --showsecret` and recreates only on content drift; needs podman ≥ 4.7, fleet-pinned to 4.9). Optional `user` (default root). Exposes `<name>_rotated` / `<name>_existed_before` for restart triggers and post-bootstrap rotation detection (e.g. Django `SUPERUSER_PASSWORD` — fail loudly, tell the operator to run `manage.py changepassword` rather than rotate silently).
- **`systemd_unit`** — `tasks_from: {install,dropin,service}`, dict `systemd_unit_args` (keys `src`, `dest`, `user`, `condition`, `start`, `restart`, `reload`, `verify`, `for`). `install` dispatches copy/template by `src` extension; `dropin` writes `<scope>/<unit>.d/override.conf` (pass `for:`); `service` pre-pulls images + starts/restarts. Drive restarts via `restart: "{{ <unit-dest>_result.changed or <other>_rotated }}"`. Always prefer over a hand-written `template:`+`systemd:` pair.
- **`systemd_timer`** — `tasks_from: {install,remove}`, `systemd_timer_args: { name, exec_start, on_calendar }` → paired `<name>.service`+`.timer` (system scope). Optional: `description`, `randomized_delay_sec`, `fixed_random_delay`, `accuracy_sec`, `on_active_sec`, `timeout_*_sec`, `persistent` (default true; set false to suppress the immediate catch-up fire on first install), `wake_system` (default false; wake from suspend to fire), `monitor` (netdata cadence-overdue), `condition` (default true; gates the whole call), `user` (needs linger), `run_as` (privilege-drop in a system unit, preferred over user-scope; canonical [roles/getmail/tasks/main.yml](roles/getmail/tasks/main.yml)), `environment` (non-secret dict), `additional_service_directives` (verbatim [Service] lines). Use for any cron-shaped job.
- **`nginx_site`** — `import_role: name: nginx, tasks_from: site`, `nginx_site_args: { subdomain, proxy_pass, … }` → `<subdomain>.<host>.<domain>` vhost with TLS/HSTS/CSP and optional Authelia. Optional: `state: absent` (tombstone), `proxy_buffering`, `client_max_body_size`, `enable_http`, `csp_*`, `auth: required`. Full key list: [roles/nginx/tasks/site.yml](roles/nginx/tasks/site.yml). Never hand-roll an nginx vhost.
- **`sqlite_dataset`** — `tasks_from: dataset`, `sqlite_dataset_args: { name, owner, group }` → creates `/mnt/services/sqlite/<name>/` and symlinks each `links[i]` dest into it, placing SQLite DBs on the dedicated 4K-recordsize ZFS dataset. Optional keys: `mode` (dir, default `0750`), `file_mode` (pre-created files, default `0640`), `condition` (check-mode gate), `pre_create` (plant empty src files for apps that migrate an empty DB — jellyfin, the \*arr stack), `force` (plant the symlink over a dangling/not-yet-created target — for apps that create their own DB on cold start, e.g. kuma/sabnzbd/tautulli; mutually exclusive with `pre_create`), `links` (list of dest paths; basenames must be unique). Both modes refuse to convert a *real* file already at a dest into a symlink (snapshot-restore safety) — the no-force path via the `file` module, the force path via a pre-stat guard; `force` only relaxes the dangling-target refusal. Canonical callers: [roles/jellyfin/tasks/main.yml](roles/jellyfin/tasks/main.yml) (pre_create), [roles/kuma/tasks/main.yml](roles/kuma/tasks/main.yml) (force).
- **`macvlan`** — list-driven per-host macvlan ifaces + optional podman networks via `macvlan_blocks:` in host_vars (schema at top of [roles/macvlan/tasks/main.yml](roles/macvlan/tasks/main.yml)). IP layout per block: lower /29 = host iface + libvirt VMs, upper /29 = podman containers (netavark ip_range, a flat pool; real gateway on the parent VLAN). Consumers attach via `--network={{ name }}`.

## Podman Service Conventions

Long-form rationale in [notes/podman_conventions.md](notes/podman_conventions.md). For new roles use `/new_podman_role`.

### Healthchecks

Every `*.service.j2` declares `--health-cmd` (and `--health-startup-cmd`), running *inside* the container. Preference order:

1. Service-native CLI — `redis-cli ping`, `mosquitto_sub`, `dig +short @127.0.0.1`.
2. `curl`/`wget` already in the image — grep the Dockerfile first.
3. Python `urllib.request` with URL in argv (python images). **JSON-array form**: `--health-cmd '["python","-c","import urllib.request as u, sys; u.urlopen(sys.argv[1], timeout=1)","http://localhost:{{ service_ports.<svc> }}/"]'`. The naïve `'u.urlopen("…")'` shape unescapes mid systemd→podman handoff.
4. `static_curl` escape hatch — distroless images. No current consumers; see notes tier 4.

### Secrets

Three paths to inject a podman secret, preferred order:

1. **App-native `*_FILE`** — `--secret=<n>,type=mount,target=<basename>` + `--env XXX_FILE=/run/secrets/<basename>`. Never lands in env. Canonical: `EMAIL_HOST_PASSWORD_FILE` in [roles/healthchecks/templates/healthchecks.service.j2](roles/healthchecks/templates/healthchecks.service.j2).
2. **linuxserver `FILE__<VARNAME>`** prefix — s6-overlay reads the file at startup.
3. **`type=env,target=VAR`** — last-resort, visible to `podman inspect`. Acceptable on single-operator hosts; prefer 1 or 2.

### User namespacing

Simplest that works. The repo grew up "uidmap everywhere" but that's inverted — tier 1 is the target for new/migrated roles ([notes/SOMEDAY.md](notes/SOMEDAY.md)).

1. **`--user {{ <svc>_user.uid }}:{{ <svc>_user.group }}`** (default). No namespace mapping; bind-mount ownership trivial. Use whenever the app doesn't insist on `id -u == 0`.
2. **linuxserver `PUID`/`PGID`** — s6-overlay aligns the baked-in `abc` user to supplied ids.
3. **Fake-root uidmap** — last resort when the image insists on `id -u == 0`: `--user 0:0` + `--uidmap=0:0:65536 --uidmap=+0:{{ <svc>_user.uid }}:1` (+ gidmap pair). Canonical [roles/healthchecks/](roles/healthchecks/). When the entrypoint then drops to a baked-in non-root uid (ClickHouse→101, MongoDB→100), allocate a dedicated host user (`service_user`, `config_dir: false`), add a `+N:<uid>:1` override, assert ownership in `_verify.yml` (canonical [roles/hyperdx/](roles/hyperdx/)).

### Prefer system-scope systemd units

Default for new timers/services is **system-scope** (`/etc/systemd/system/`). Reach for user-scope (linger) only when fundamentally required — today just rootless podman ([roles/gitea_runner/](roles/gitea_runner/), [roles/github_runner/](roles/github_runner/)). One operational vocabulary; netdata's `systemdunits` collector attaches to the system bus only; single privilege-drop model (`User=`/`Group=` or `systemd_timer`'s `run_as:`). When hardening, `systemd-analyze security <unit>` scores ~50 sandboxing directives and lists cheap wins — run it against any new `.service.j2` (syntax validation is already automated by `systemd_unit`'s `verify:`).

### Inter-container DNS

Containers reach a co-located podman service by its **`<name>.dns.podman` FQDN** (aardvark-dns), never a host port or hard-coded IP. The producer creates a per-service `containers.podman.podman_network` with `disable_dns: false` — the flag only wires DNS under the **netavark** backend ([roles/podman](roles/podman)); on CNI it no-ops without the dnsname plugin. Each consumer joins via `--network <name>` and connects to `<name>.dns.podman`. Both producer and consumer create the network independently (it's idempotent), so each role stays self-contained. The bare short name `<name>` resolves too, but the FQDN is the canonical form in connection strings (z2m → `mqtt://mosquitto.dns.podman:1883`). Canonical: [roles/redis/](roles/redis) (producer) + [roles/mosquitto/](roles/mosquitto) / [roles/z2m/](roles/z2m) (consumer). Target the network name or gateway IP, never an `ethN` index — interface ordering isn't stable across hosts.

## Testing Guidelines

The harness lives in `test/` (Python, asyncio).

### Harness CLI

`test/testrole.py <role>` boots `box` (default) and applies the role end-to-end. `test/testall.py` fans out role × machine in parallel. Load-bearing flags: `--machine {minimal,box,lab,pug}`, `--keep` (leave VM up for SSH debug), `testall.py --retry-failed`. Exit codes: `0` success, `1` converge, `124` timeout, `125` idempotence, `130` cancelled. Output → `test/out/<machine>.<role>.ansi` (journal + last 50 lines tailed on failure). Artifact trees under `/mnt/scratch/qemu/<codename>/` (Linux) or `packer/artifacts/<codename>/` (Mac); macOS needs `xorriso` for the cloud-init seed iso.

**A flake must fail fast, not inflate CI duration.** Transient first-boot hiccups (sshd slow to bind, a half-open SLIRP hostfwd) are a fact of life on the qemu fixtures. The contract: every wait in the harness (`ensure_booted`/`ensure_ssh`, banner probe, socket close) is **bounded** so a stuck boot surfaces as a quick failure (~the SSH deadline) the operator re-runs — never a silent hang that burns the full per-cell `timeout` and makes a green-on-retry cell look like it *took* 25 min. Don't paper over flakes with auto-retry (it hides a real regression behind a re-roll); fix the unbounded wait that let the flake balloon, and keep the boot diagnosable (the ZBM stage logs to serial — see [packer/scripts/chroot.sh](packer/scripts/chroot.sh)).

### Debugging prod hosts directly

SSH to `lab`/`pug`/`bunk` works out of the box (key+sudoers configured). For investigation — service logs, daemon state, hardware probes, on-disk config — connect and run diagnostics yourself rather than asking the operator to paste output (`ssh lab 'sudo fan2go detect'`).

**Authorization boundary.** Diagnostic reads on read-only state are pre-authorized. These need explicit ack each time (per Hard Rules):

- Anything mutating: `systemctl restart`/`reset-failed`, `nft -f`, `apt`/`dnf` (even `list` — network egress), editing config, anything under `/mnt/services/*/secrets/` or `/etc/credstore*`.
- Anything that *exfiltrates secrets into the transcript*: `journalctl -u <svc>` for credential-logging services (`postfix`, `getmail`, `*_runner`, `healthchecks` mail path), `podman secret inspect`, reading vault files, env dumps.

When in doubt, ask. **Bridge to ack:** before requesting ack for a mutating change, run `mise run ansible --limit <host> --tags <role> --check` first — `--diff` is on by default, so the operator sees the would-be diff alongside the ack request.

### Test environment design

Axis-collapse rationale + ZBM-aarch64 workaround in [notes/test_environment_design.md](notes/test_environment_design.md).

**Packer images exist only for the qemu test fixtures.** Prod hosts (`lab`/`pug`/`bunk`/`fox`) are configured by ansible from a stock Ubuntu install — no image bakes in their setup, and `packer/scripts/chroot.sh` is the test-fixture analogue of bootstrap/role logic, not a source of truth for prod. The `boot` role owns the boot chain that isn't hardware bootstrap: a shared kernel-cmdline engine (`import_role: name: boot, tasks_from: cmdline` with `boot_cmdline_args: {name, contents, grub_scope, grub_name}` — dispatches grub.d vs the ZBM `org.zfsbootmenu:commandline` property by `grub_bootloader`; `zfs`/`console`/`kdump` each contribute one fragment), plus the grub hold, `MODULES=most`, ESP tooling, and base console args. `zfsbootmenu` downloads the loader image; `refind` owns `refind.conf` + theme + the rEFInd binary (copied from the package, x86 only) + the `/EFI/BOOT` fallback. Still hand-established at bootstrap (not role-owned): per-disk NVRAM `efibootmgr` entries and disk/ESP/mdadm creation. When a role and chroot.sh both write boot state (refind.conf, ZBM image, cmdline) they are hand-synced twins — change both.

| Variant     | Disks                                                       | Use case                                                          |
|-------------|-------------------------------------------------------------|-------------------------------------------------------------------|
| `minimal`   | Vanilla cloud image, ext4                                   | Stranger-baseline; opt-in via `machines:` in `meta/test.yml`      |
| `box`       | Single-disk rpool, no extra pools                           | Default push-CI fixture; producers run flat under rpool/          |
| `box_deps`  | `box` + pre-baked podman + nginx                            | Opt-in for podman-service roles; saves ~140s/test                 |
| `pug`       | 1 rpool + 2 apoc                                            | Matches pug prod; on-demand + nightly                             |
| `lab`       | mdadm-EFI + 3-disk mirror rpool + dozer/tank(raidz2)/mouse  | Matches lab prod; on-demand + nightly                             |

Push CI fans out only to `box` (and `minimal` for roles that list it in `machines:`). Nightly runs the full universe. Editing pool topology requires a packer rebuild (15-30 min). `box_deps` is a **derivation** of `box`: `mise run packer:seed-deps --ubuntu <codename>` clones box artifacts, applies [packer/seed_deps.yml](packer/seed_deps.yml), publishes as `box_deps` (rebuild after every `packer:build box --ubuntu <codename>`). Built per release (jammy/noble/resolute). A role opts in via `machines: {box_deps:}` in `roles/<role>/meta/test.yml` (honoured unless `--machine` is passed); reuses `host_vars/box.yml`.

## Continuous Integration

GitHub Actions on lab via `github_runner` (templated `github_runner@<repo>_<suffix>.service`, one per entry in [host_vars/lab.yml](host_vars/lab.yml)'s `github_runner_instances`). Internals: [notes/github_runner_design.md](notes/github_runner_design.md).

Workflows in [.github/workflows/](.github/workflows/):

- `lint` — every push; `mise run lint` in the ci container.
- `test` — every push; `detect` fans out per-role via `mise run ci:detect-roles`. Cross-cut paths (`group_vars/all/main.yml`, `host_vars/{box,minimal}.yml`, `test/*.py`, `mise.toml`) emit an empty matrix + mail instructions.
- `test-nightly` — `0 2 * * *` + dispatch; full-universe matrix. Skips when no commits in 25h.
- `packer-build` — call-only; ci.yml invokes it (gated on detect's `packer_changed`) with the source matrix detect computed in `sources_matrix`. Only writer of `/mnt/scratch/qemu`. Shares the `lab-qemu-artifacts` concurrency group; flock details [notes/concurrency_rework.md](notes/concurrency_rework.md). Standalone rebuild of just some sources: dispatch `ci` with `sources=lab pug` (detect makes it a packer-only run, empty test matrix).
- `ci-image` — dispatch + push to `master` touching Dockerfile inputs; rebuilds `nexus.lab.fahm.fr/homelab/ci:<sha>`+`:latest`. Runs *outside* the ci container; dispatch once to bootstrap a fresh runner.

**Variant escalation** — adding `minimal:` to a role's `machines:` dict in `meta/test.yml` adds a `(role, minimal)` entry; only when behaviour depends on upstream-shipped packages (each entry doubles cost). **Release escalation** — a role's `meta/test.yml` `ubuntu:` list adds a `(role, machine, <codename>)` cell for each machine × release cross-product, so a change to a release-gated role is validated on those releases across all its machines. Only list releases where the converged result diverges from the jammy default cell (e.g. `apt_source: [resolute]` for its `>= 26` deb822 branch); box_deps is built per release via `mise run packer:seed-deps --ubuntu <codename>` (rebuild after each `packer:build box --ubuntu <codename>`). On a push these release cells **propagate down the role-deps fan-out**: changing a release-gated helper (e.g. `apt_source`) adds its release cell to every consumer that imports it, not just the helper's own standalone cell — so dependents are exercised against the helper's release-specific path. (Propagation is push-only; `--all`/nightly and explicit `roles=` dispatch give each role only its own declared releases.) **Runner pools** — VM workloads on `lab-vm`, else `lab` (disjoint). **CI secrets** are terraform-managed (rotation [notes/ci_secrets_runbook.md](notes/ci_secrets_runbook.md); the GitHub provider auths via the operator's `gh auth token`, never exposed to CI). The `gitea_runner` role ships a Gitea-side runner for ad-hoc workflows; this repo's CI doesn't use it.

**Local-debug recipes:** manual subset via Actions → `test` → Run workflow → `roles=foo,bar:lab,baz:minimal` (`roles=ALL` = everything; bare `foo` → `foo:box` + `foo:minimal` if in its `machines:`); `CI_BASE_REF=HEAD~5 mise run ci:detect-roles` previews a multi-commit push matrix; `GITHUB_EVENT_NAME=workflow_dispatch INPUTS_ROLES=foo,bar mise run ci:detect-roles` previews a dispatch; `mise run ci:role-deps <helper>` lists consumers of a helper role.

## Commit & Pull Request Guidelines

Descriptive imperative subjects; prefix with a role when it helps (`github_runner: drop --runnergroup arg…`). Body: summary + motivation + reviewer context, max two paragraphs.

**Splitting a multi-change worktree:** use `/split_worktree_commits`. Manual: drive `git add --patch=<file>` non-interactively via `printf 'y\nn\nn\n' | git add -p <file>` (`s` first to split hunks). **Always `git diff --staged` and verify before `git commit`.** Don't revert the worktree to redo edits from scratch. **Never stage a vault-rendering template hunk-by-hunk** — files with rendered secrets (`templates/*.j2` referencing `*_password`/`*_token`/`*_secret`) must be staged whole-file or not at all (a misordered hunk can silently mix old/new secret states).

## Security & Configuration Tips

Don't commit decrypted data; access secrets via `ansible-vault edit <path>`, keep them in `group_vars/*.yml`/`host_vars/*.yml`. WireGuard keys in `wireguard/` stay vaulted and rotate with peer changes. Touching networking/DNS: run with `--limit`, apply Terraform only after review in a dedicated branch.

### Vault ids: `prod` vs `test`

Two passwords, two scopes ([ansible.cfg](ansible.cfg): `vault_identity_list = prod@vault-client.sh, test@vault-client.sh`, `vault_id_match = True`).

- `prod` — encrypts `group_vars/prod.yml` + prod host_vars (lab/pug/bunk). Local workstations only; never in CI.
- `test` — encrypts `group_vars/test.yml` + test host_vars (currently `host_vars/box.yml`). Available to CI as GitHub repo secret `HOMELAB_VAULT_PASSWORD_TEST`, so reachable to any workflow run — never put a prod-blast-radius credential there.

[vault-client.sh](vault-client.sh) follows ansible's "client" protocol (filename ends `-client`). Lookup per id: env `HOMELAB_VAULT_PASSWORD_<UPPER_ID>` (CI), then macOS keychain `homelab-vault-<id>`, then Linux `~/.config/homelab/vault-pass-<id>` (0400). Bootstrap: [notes/vault_setup.md](notes/vault_setup.md). New values: `encrypt_string --encrypt-vault-id prod` (or `test`) for the right label. Re-label: `ansible-vault rekey --new-vault-id <new>@vault-client.sh <path>`.

## Someday

Open follow-ups live in [notes/SOMEDAY.md](notes/SOMEDAY.md) — backlog notes, not standing instructions; don't enact one without explicit operator confirmation.
