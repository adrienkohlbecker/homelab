# Repository Guidelines

## Spirit & Trade-offs

This is a home environment, not a corporate production site. Tie-breakers when the Hard Rules don't decide a question:

- **Maintainability beats ambition.** The operator is also the on-call — an elegant rewrite they can't debug at 11pm is a regression. Simple problems get simple solutions; complex ones may warrant structure — a layered role system, a multi-stage pipeline — but it must stay coherent and followable, expressing rather than obscuring what it does.
- **Security is pragmatic.** Reasonable hygiene (vault, firewall, file permissions) is in scope; defense against nation-state actors is not. New security machinery needs a concrete threat behind it.
- **Wife-acceptance-factor is real.** Household-depended services (home-assistant, z2m, media, dns) have higher cost-of-failure than operator-only infra. Visible breakage outweighs elegance.
- **Functional tests over stat checks.** Exercise code and configuration against a real running system; a stat-only check (file exists, service enabled, configuration set) proves the role ran, not that the service works.
- **Not everything must be codified.** Codify the *platform* — the service, its reverse proxy, its secrets, its backups. But state that lives in a service's own UI/DB — Uptime-Kuma monitors, Healthchecks checks — is an accepted exception: configure it in the app and let it ride the ZFS snapshots, with the intent captured in a runbook. Don't contort Ansible (fragile sqlite seeds, unofficial APIs) to own monitor definitions.

## Hard Rules — DO NOT

Load-bearing negatives, up-front so a fresh session sees them first.

- **DO NOT use Ansible handlers for service restarts.** Handlers run at end-of-play and break the required ordering between image pulls, unit writes, and lifecycle changes. Repository-owned units use `systemd_unit`'s inline `tasks_from: unit`; package-owned units use role-local `systemd` tasks. Drive restart/reload state from registered `*.changed` results. See *Helper roles → systemd_unit*. (lint: `no-handlers`)
- **DO NOT drop a container's `--health-cmd`** in favour of external monitoring (kuma, `_verify.yml`). Without an in-container check, `--sdnotify=healthy` can't gate the unit's `active` state and podman won't auto-restart on quiet HTTP failure. See *Healthchecks*.
- **DO NOT default required service inputs in `vars/main.yml`** — role vars sit *above* inventory vars in ansible's precedence ladder and silently mask host-level overrides. Required inputs live in inventory vars (see *Inventory layout*) and the role must `assert:` they're set. `defaults/main.yml` is fine for optional host-overridable values since it sits *below* inventory vars. Canonical: [roles/gitlab_runner/defaults/main.yml](roles/gitlab_runner/defaults/main.yml).
- **DO NOT run state-mutating commands on prod hosts (`lab`/`pug`/`bunk`) without explicit ack.** Diagnostic SSH is pre-authorized; mutations are not. See *Production*.
- **DO NOT add tautological checks to role `_verify.yml` files.** A check that only confirms a converge task wrote the requested file or value proves nothing beyond Ansible's own result. Exercise the consuming binary or service and assert observable behavior; if no meaningful functional assertion is possible, omit the check.

## Development Commands

Repository map: [README.md](README.md).

- Bootstrap: install [mise](https://mise.jdx.dev), `mise trust`, then `mise install`. `python.uv_venv_auto` auto-sources `.venv`; `uv sync` populates Python deps. 1Password CLI must be signed in for the `op://` env vars in `mise.toml` to resolve.
- **op:// env refs only resolve under `op run --`.** Toml tasks wrap explicitly; file-based tasks under `mise-tasks/` do **not** — mise exports the literal `op://…` string. Fix: re-exec under `op run --` behind a guard env var.
- Lint: `mise run lint` (ansible-lint, tofu/packer fmt+validate, tflint, ruff/pyright, yamllint, shellcheck+shfmt, stylua+selene, taplo, markdownlint — all parallel); `mise run fmt` applies fixes (`fmt:ansible` = `ansible-lint --fix` — prefer over hand-editing). Inner-loop: prefer `mise run lint:ansible-changed` (~4s; override base via `LINT_BASE=<ref>`) over full `lint:ansible` (~40s). Run full `mise run lint` before pushing.

## Workflows — use the skill, don't reinvent

- `/triage <service>` — investigate a service end-to-end (resolve host(s), gather state, summarize).

Skill and hook wiring for both agents: [.agents/README.md](.agents/README.md).

## Coding Style & Naming Conventions

**Underscores, not hyphens** in identifiers we author: role names, files under `roles/`, systemd units, vars, dirs under `/mnt/services/<svc>/`. Exceptions: names dictated by upstream. Everything else enforced by `mise run lint`.

**Never set `no_log: true`.** Applies run interactively on the operator's workstation, never captured to a file or CI log — hiding the diff only makes failures harder to debug. (lint: `no-no-log`)

**Shell strict mode is enforced for Ansible shell blocks.** Inline ansible `shell:` blocks must start with `set -euo pipefail` and declare `executable: /bin/bash`. Enforced by the custom **`shell-strict-mode`** rule ([lint/ansible_rules/homelab.py](lint/ansible_rules/homelab.py)); test scaffolding (`_verify*`/`_setup*`) exempt. Handle expected-failure commands with `|| true` and avoid `… | head` pipelines (SIGPIPE) — collapse into `awk`. **No apostrophes inside `shell:`/`command:` block scalars** — even in `#` comments within the block; ansible's pre-exec shlex pass treats `'` as an opening quote and fails task loading. YAML-level comments (outside the scalar) are fine.

**Packer inline shell stays tiny.** Keep Packer `inline` shell blocks to one or two simple commands. Longer provisioner or post-processor scripts belong in checked-in `.sh` files under `packer/scripts/` or the relevant Packer subdirectory, with `set -euo pipefail`, so shellcheck/shfmt cover them and the HCL stays declarative.

## Repo Conventions

### Role layering (`site.yml`)

`site.yml` is ordered as a **layer ladder** — a role's converge position *is* its layer, and each layer builds on the guarantees of the ones above. Two bands:

- **Base machine install** (`hosts: lab,pug,fox`) — sub-bands: *host base* (OS, networking, access), *persistent state* (`services` before its SSH identity consumers), *storage & boot* (`zfs`/`zfs_autobackup`/`zfsbootmenu`/`refind`), *service platform* (`podman`, `certbot`, `nginx`), *observability* (`netdata`/`fluentbit`). Roles within host-base are mutually independent.
- **Services** — host-scoped plays that assume the full platform is already in place.

**Where does a new role go?** Stop at the first layer whose guarantees you need: booted OS → host base; ZFS → after storage; podman/nginx-TLS → after platform (the base⇄service watershed); app for subset of hosts → service play. Keep dataset *producers* ahead of consumers.

### Inventory layout

`hosts.ini` (prod) and `test/inventory.ini` (qemu fixtures) share host names — the `lab`/`pug` fixtures take their prod host's identity — so vars are split by which inventory may load them:

- `group_vars/all/`, `group_vars/{prod,test}.yml` — shared defaults and per-environment values.
- `group_vars/physical_<host>.yml` — one prod host's hardware and service settings. Only `hosts.ini` defines these groups, so fixtures never load them.
- `group_vars/storage_<host>.yml` — disk layout shared by a prod host and its fixture (pools, swap, podman device). Both inventories define the group.
- `test/host_vars/<host>.yml` — fixture-only settings, loaded only through `test/inventory.ini`.
- Root `host_vars/` — only hosts absent from the test inventory (`bunk`). The harness copies it beside every playbook, so a `host_vars/lab.yml` would leak prod settings into the Lab fixture; [unit_tests/test_inventory_layout.py](unit_tests/test_inventory_layout.py) rejects it.

### Role conventions

**Load-bearing idioms** — these break silently if missed:

- Gate test-only branches on `qemu_test`, set by the test inventory's `test` group.
- Per-role test hooks live alongside the role's tasks:
  - `tasks/_setup.yml` — pre-role fixture bringup. **Never runs against prod.**
  - `tasks/_verify.yml` — post-converge assertions. **Only invoked by the harness against a qemu VM** — never against prod. That contract lets scaffolding sit alongside real tasks without a `when: qemu_test` gate. Rebooting inside `_verify` is fine for next-boot state (canonical [roles/console/tasks/_verify.yml](roles/console/tasks/_verify.yml)).
- **Check mode:** `--check` predicts user, group, and unit creation without exporting numeric ids. Gate consumers of a fresh `<svc>_user`'s ids or directories on `not ansible_check_mode or <svc>_user.uid is defined` (a real run must never silently skip), and start/restart on `not (ansible_check_mode and (...changed))` over the pending user/unit results. `ansible_check_mode` reflects only the CLI flag: under task-level `check_mode: true` in `_verify` these guards don't fire, so import only the task file under test, assert on its published results, and keep `check_mode: false` out of that file unless something else gates it.
- Prefer `import_role`/`import_tasks` over `include_*`. Fall back to `include_*` only for genuinely dynamic name/vars, then wrap the loop body in a per-iteration `include_tasks` for fresh scope.
- Static fixture playbooks under `test/playbooks/` must set `tasks_from` on every `import_role`. Their dependencies are named role entrypoints, never an implicit `tasks/main.yml`; the dynamic `site.yml` driver is the exception because it deliberately exercises the role under test through its normal entrypoint. (lint: `require-named-role-entrypoint`)
- For state-mutating tasks that should run once, gate with `args: creates: <sentinel>` not `changed_when: false`.
- **Never branch a task `when:` on `inventory_hostname`** — a new host silently misses a hardcoded list. Set a `<svc>_enabled` flag in the host's inventory vars and read it as `<svc>_enabled | default(false)`. Play-level `hosts:` patterns in `site.yml` are the legitimate exception. (lint: `no-inventory-hostname-when`)

**Style:**

- Every task within a role carries the role name as a tag, even when the role invocation is already tagged, so task files stay independently reusable. Test hooks (`_setup`/`_verify`) and helper roles are exempt; helper callers supply the tag scope.
- Centralize every pinned upstream version — container image tags, downloadable artifacts (debs/tarballs/binaries), and package-manager pins — in [group_vars/all/versions.yml](group_vars/all/versions.yml): full URL **adjacent to its sha256**, keyed by `ansible_architecture` (`x86_64`/`aarch64`) for multi-arch assets. Roles consume the pin by var name (ansible resolves vars globally); a role's `vars/main.yml` keeps only derived/computed values (e.g. arch-selected binary names, paths built from a version), never the raw pin.
- Every config-writing `copy:`/`template:` carries a best-effort `validate:` that parses the rendered file. Omit rather than invent a fake one. Safe as `validate:` args but **don't** lift into `command:`/`shell:` — embedded quotes break task loading.
- **Preserve upstream commentary and defaults in configuration derived from an upstream reference.** Keep comments, examples, and explicit default-valued settings intact so the file remains recognizable and comparable with upstream. This is a deliberate exception to sparse comments and the locally-authored rule below: the upstream reference *is* current state for the file.
- **In locally-authored configuration, don't pin a value just because it equals the upstream default.** Pin only when load-bearing: documents a dependency, is a deliberate non-default, or is a tuning knob worth surfacing.
- Every file-writing task sets **`backup: true`** — enforced by the `require-backup` rule ([lint/ansible_rules/homelab.py](lint/ansible_rules/homelab.py)); test scaffolding and `_`-roles exempt. Exceptions carry `# noqa: require-backup`. **`backup: true` is safe even on secret-rendering templates** (getmail, wireguard, authelia, minio, …) — keep it there too. The `<file>.<pid>.<ts>~` backups inherit the source file's mode (so a 0600 secret stays 0600), are pruned host-wide after 7 days by [roles/cleanup](roles/cleanup) (`prune_ansible_backups` daily timer, by filename timestamp under `/` + `/mnt/services`), and are excluded from the offsite replica by the `*@*:*:*~` rule in [roles/zfs_autobackup/files/zfs_backup_offsite.sh](roles/zfs_autobackup/files/zfs_backup_offsite.sh). So no secret history accumulates on disk or leaves the fleet.
- `/mnt/services/<svc>/` is service *state* (configs, DBs, secrets — rides ZFS snapshots). Service *code* from the repo belongs at `/opt/<svc>/`. Canonical: [roles/homepage/tasks/main.yml](roles/homepage/tasks/main.yml).

### Service ports

Ports live in `group_vars/all/main.yml` under `service_ports:` — single source of truth. **Scope: only operator-reachable ports** (host-published `--publish` or loopback binds). Container-to-container traffic over podman networks stays as inline literals. When allocating a new port, check `service_ports:` for collisions.

### ZFS site mountpoints

Per-site dataset gates in `group_vars/all/main.yml` under `zfs_has_<name>_mount:` (services/scratch/media/data/minio). **Producers** create datasets unconditionally and **never read the flag**. **Consumers** gate on the flag for bind-mounts. Test fixtures flip every flag `false`. Don't gate the producer — it would no-op under the default test machine.

### Homepage bookmarks

New user-facing services get a bookmark in [roles/homepage/templates/bookmarks.yaml.j2](roles/homepage/templates/bookmarks.yaml.j2) — follow the `abbr: XX` + `icon: sh-<name>.png` + `href: https://<subdomain>.{{ inventory_hostname }}.{{ domain }}/` shape. Icons resolve through selfh.st (`sh-` prefix).

### Home Assistant GUI YAML sync

Drive with `mise run ha:sync [pull|push|sync]` ([mise-tasks/ha/sync.py](mise-tasks/ha/sync.py)). Don't bypass the sync (no `scp`, no live-VM editing). The files live in `roles/homeassistant/files/ha_gui_config` — an **in-place gitignored clone** of the private `homelab_ha_config` repo (not a submodule); `ha:sync` owns it (commits + pushes there, deploys to the HA host). The HA role only creates dirs + include-target stubs.

### Home Assistant `.storage` config

For config HA has moved out of YAML, pick the highest tier that works — `configuration.yaml.j2`, then a `force: false` seed of a single-purpose `.storage/<key>`, then a reconcile into `.storage/core.config_entries` (smtp only; **HA must be stopped before that write**) — and record the choice in [notes/runbooks/homeassistant_gui_config.md](notes/runbooks/homeassistant_gui_config.md), which holds the tier rationale.

### `notes/` private clone

`notes/` is a gitignored, independent clone of the private [adrienkohlbecker/homelab_notes](https://github.com/adrienkohlbecker/homelab_notes) — not a submodule. Notes content must never land in this public repo. Commit and push inside `notes/` (a hook stamps each commit with a `Code: homelab@<sha>` trailer); in worktrees `notes/` is a symlink to the main checkout's single clone. Every note opens with YAML frontmatter carrying `status` and `created_at` (enforced by the notes pre-commit hook). Statuses: `runbook` (active procedures) · `current` (deployed state) · `planned` (near-term) · `deferred` (valid, not imminent) · `rejected` (kept for context) · `completed` (outcome in code/git) · `reference` (static lookup). Archived notes live in `notes/archive/`.

### Helper roles

Prefer these over re-implementing boilerplate. Call them with `tasks_from` and a single `*_args` dict, passing any `condition` inside it rather than as `when:` on the import. Authoring contract (empty `main.yml`, `condition`, removal entry points, tag scope) and full API: [notes/helper-roles-reference.md](notes/helper-roles-reference.md).

| Helper | Entry point | Exposes / creates |
|--------|-------------|-------------------|
| `service_user` | `tasks_from: user` | `<svc>_user.uid`/`.gid`; `/mnt/services/<svc>` dir |
| `systemd_unit` | `tasks_from: {install,unit,remove}` | unit/drop-in install results; repository-owned unit lifecycle and removal |
| `systemd_timer` | `tasks_from: {install,remove}` | paired `.service`+`.timer` units |
| `apt_source` | `tasks_from: {configure,remove}` | deb822 source + scoped signing key + optional series pin; `apt_source_<name>_changed` |
| `nginx_site` | `import_role: name: nginx, tasks_from: site` | vhost with TLS/HSTS/CSP + optional Authelia |
| `boot` | `import_role: name: boot, tasks_from: {cmdline,initramfs_provider,initramfs_rebuild}` | kernel cmdline fragment; release image generator + policy; image rebuild with an optional reboot request (boot's `main.yml` is a real role entry point) |
| `zfs_dataset` | `tasks_from: dataset` | ZFS filesystem + mount unit, or a plain mountpoint on non-ZFS hosts |
| `macvlan` | driven by `macvlan_blocks:` in inventory vars | ifaces + optional podman networks |

## Podman Service Conventions

Long-form rationale in [notes/podman_conventions.md](notes/podman_conventions.md). A new service role mirrors a recent sibling and wires `service_ports:`, its `site.yml` play, an `nginx_site` vhost, and a homepage bookmark. **Use canonical upstream image names** in service templates (`docker.io/sonatype/nexus3:3.91.1`, not `nexus.lab.fahm.fr/docker.io/…`); mirror redirection belongs in `registries.conf` + the `--upstream-mirrors` test flag.

### Healthchecks

Every `*.service.j2` declares `--health-cmd` (and `--health-startup-cmd`). Preference order:

1. Service-native CLI — `mosquitto_sub -t ...`, `dig +short @127.0.0.1`.
2. `curl`/`wget` already in the image — grep the Dockerfile first.
3. Python `urllib.request` (python images). **Must use JSON-array form** to survive systemd→podman handoff: `--health-cmd '["python","-c","import urllib.request as u, sys; u.urlopen(sys.argv[1], timeout=1)","http://localhost:PORT/"]'`.

No distroless image is in service today; if one arrives needing a healthcheck, bind-mount a statically-linked curl into the unit (JSON-array `--health-cmd` — the image has no `/bin/sh` for the string form).

### Secrets

Three paths, preferred order:

1. **App-native `*_FILE`** — `--secret=<n>,type=mount,target=<basename>` + `--env XXX_FILE=/run/secrets/<basename>`. Never lands in env.
2. **linuxserver `FILE__<VARNAME>`** prefix — s6-overlay reads the file at startup.
3. **`type=env,target=VAR`** — last-resort, visible to `podman inspect`.

### User namespacing

1. **`--user {{ <svc>_user.uid }}:{{ <svc>_user.gid }}`** (default). No namespace mapping. Use whenever the app doesn't insist on `id -u == 0`.
2. **linuxserver `PUID`/`PGID`** — s6-overlay aligns the baked-in `abc` user.
3. **Fake-root uidmap** — last resort: `--user 0:0` + `--uidmap=0:0:65536 --uidmap=+0:{{ <svc>_user.uid }}:1`. Canonical [roles/jellyfin/](roles/jellyfin/). When the entrypoint drops to a baked-in non-root uid, allocate a dedicated host user and add a `+N:<uid>:1` override (canonical: [roles/nexus/](roles/nexus/)).

### PID 1 / `--init`

Add `--init` (right after `--name`) **only when the image runs the application directly as PID 1** — a bare Go/Java/.NET/node binary as `ENTRYPOINT`; podman's `catatonit` then reaps children and forwards `SIGTERM`. Canonical: [roles/adguard/](roles/adguard/), [roles/jellyfin/](roles/jellyfin/). Skip it when the image ships its own init — s6-overlay `/init` (every `linuxserver/*`, home-assistant), `dumb-init`/`tini`, or an entrypoint that `exec`s a supervisor. Check, don't guess: `sudo podman image inspect <img> --format '{{json .Config.Entrypoint}}'`, and read the script if it's a shell wrapper. Rationale: [notes/podman_conventions.md](notes/podman_conventions.md).

### Prefer system-scope systemd units

Default for new timers/services is **system-scope**. Reach for user-scope (linger) only when fundamentally required. When hardening, `systemd-analyze security <unit>` lists cheap wins.

### Inter-container DNS

Containers reach co-located podman services via **`<name>.dns.podman`** (aardvark-dns), never a host port or hard-coded IP. Both producer and consumer create the `containers.podman.podman_network` independently (idempotent). Requires the **netavark** backend with `disable_dns: false`. Canonical: [roles/mosquitto/](roles/mosquitto) (producer) + [roles/z2m/](roles/z2m) (consumer). Target network name or gateway IP, never an `ethN` index.

## Testing

The harness lives in `test/` (Python, asyncio). Fixtures: `lab` (default; Lab-style mirrored root plus data pools), `pug` (Pug's single-rpool partitioning and apoc layout), and `minimal` (the downloaded vanilla cloud image, for non-ZFS, GRUB, cloud-init, and fresh-install branches). The Packer-built `lab`/`pug` images carry only the base OS and storage layout — each cell's `_setup.yml` installs role dependencies — and refresh with `mise run packer:build [lab|pug]` (`--ubuntu resolute` for another release). Details: [notes/test_environment_design.md](notes/test_environment_design.md).

- `test/testrole.py <role>` boots `lab` (default) and applies the role end-to-end; `test/testall.py` fans out role × machine in parallel. Flags: `--machine {minimal,lab,pug}`, `--keep`, `testall.py --retry-failed`. Exit codes: `0` success, `1` converge, `124` timeout, `125` idempotence, `130` cancelled. Failed-run artifacts → `test/out/<machine>.<ubuntu>.<role>.*.ansi`.
- Replay a kept fixture with the `ansible-playbook` command printed by `testrole.py --keep`; add `--start-at-task 'TASK NAME'` or `--step` to resume within the phase. The command uses the staged playbook and roles, so rerun `testrole.py` after editing repository code. `mise run ansible` targets production and must not be used for fixture recovery.
- **Flake policy:** every wait in the harness is **bounded** — a stuck boot surfaces as a quick failure, never a silent hang. Don't paper over flakes with auto-retry; fix the unbounded wait.

## Continuous Integration

GitLab CI ([.gitlab-ci.yml](.gitlab-ci.yml)) runs the role-test matrix as **qemu cells**. The `HOMELAB_CI_TARGET` pipeline variable picks where they run: `aws_qemu` (default — nested KVM on autoscaled AWS shell hosts, hydrating promoted images from S3) or `lab` (lab's shell runner, booting images straight from `/mnt/scratch/homelab_ci`). The `.qemu_image` bake always runs on `lab-shell-qemu`, the sole KVM builder, and uploads each build to S3 for the AWS cells. `detect` ([mise-tasks/ci/detect.py](mise-tasks/ci/detect.py)) classifies the diff into a child pipeline of one `test_cells` job per `role:variant[:ubuntu]` cell; `lint`, `unit_tests`, and other stateless jobs run on GitLab-hosted runners. Design (image promotion, retention, runner tags): [notes/ci_aws_nested_qemu_cells.md](notes/ci_aws_nested_qemu_cells.md).

**Escalation:** A nonempty `machines:` map replaces the default Lab cell. List `lab:` alongside `minimal:` or `pug:` when Lab coverage should remain; the first machine is the local `testrole.py` default. `machines: {lab: {memory_mb: 5120}}` raises that cell's guest RAM above the `test/machine.py` default; `ubuntu:` lists add per-release cells. **Manual dispatch:** a `ROLES` pipeline variable bypasses change detection — `ROLES=ALL` runs the whole universe, a comma-separated list runs those cells, and the `SITE` token adds the full-fleet converge (`ROLES=SITE` runs it alone, `ROLES=SITE,nginx` pairs it with a cell). **Local-debug:** `CI_BASE_REF=HEAD~5 mise run ci:detect --child-path /tmp/cells.yml` previews the cell matrix (logged to stderr). CI secrets: [notes/runbooks/ci_secrets.md](notes/runbooks/ci_secrets.md).

## Production

- **Access:** SSH to `lab`/`pug`/`bunk` for diagnostics, including service logs, is pre-authorized. Anything mutating (`systemctl restart`, `apt`, config edits, `/mnt/services/*/secrets/`) or exposing secret material (`podman secret inspect`, vault files, secret files/env) needs explicit ack — run `mise run ansible --limit <host> --tags <role> --check` first so the operator sees the diff.
- **Apply:** `mise run ansible --limit <host> [--tags <role>]`, or `--limit prod` for the fleet (wrapper handles vault-id, ssh args, env; `ansible.cfg` binds the repo to `hosts.ini` and `vault-client.sh`, so run from the root).
- **Terraform:** `mise run tf {init,plan,apply}` — `cd`s into `terraform/` and forwards to `tofu` (use `--` for flags mise intercepts). `apply` only after the operator has reviewed the plan. State in MinIO (`s3://terraform/homelab.tfstate`), AES-GCM-encrypted. Rotation: [notes/runbooks/terraform-state-encryption-rotation.md](notes/runbooks/terraform-state-encryption-rotation.md).
- **Logs:** pipe `journalctl` through `lognorm` ([roles/user](roles/user/files/lognorm)), which masks volatile tokens and aggregates repeats so dominant noise and rare errors both surface: `journalctl --since "1 day ago" -p warning | lognorm`, `journalctl -u <svc> -b 0 | lognorm --top 20`.

### Vault ids: `prod` vs `test`

Two passwords, two scopes:

- `prod` — vault id for inline `!vault` values in `group_vars/prod.yml`, physical-host vars, and root `host_vars/` (including Bunk). Local workstations only; never in CI.
- `test` — vault id for inline `!vault` values in `group_vars/test.yml` and test-host vars. Available to CI as `HOMELAB_VAULT_PASSWORD_TEST` — never put a prod-blast-radius credential there.

New values: `encrypt_string --encrypt-vault-id prod` (or `test`). Never commit decrypted values. Password lookup and bootstrap: [notes/runbooks/vault_setup.md](notes/runbooks/vault_setup.md).

## Someday

Open follow-ups live in [notes/SOMEDAY.md](notes/SOMEDAY.md) — backlog notes, not standing instructions; don't enact one without explicit operator confirmation.
