# Repository Guidelines

## Hard Rules — DO NOT

Load-bearing negatives, listed up-front so a fresh agent session sees them before the wall of detail.

- **DO NOT use Ansible handlers for service restarts.** Handlers run at end-of-play, which doesn't compose with the `systemd_unit` helper's pre-pull-then-start ordering. Drive restarts via the helper's inline OR chain on `*_result.changed` / `*_rotated`. See *Helper Roles → systemd_unit*.
- **DO NOT drop a container's `--health-cmd`** in favour of external monitoring (kuma, `_verify.yml`). Without an in-container check, `--sdnotify=healthy` can't gate the unit's `active` state and podman won't auto-restart on quiet HTTP failure. See *Podman Service Conventions → Healthchecks*.
- **DO NOT default required service inputs in `vars/main.yml`** — role vars sit *above* host_vars in ansible's precedence ladder and silently mask host-level overrides. Required inputs live in `host_vars`/`group_vars` and the role must `assert:` they're set. `defaults/main.yml` is fine for optional host-overridable values (e.g. `fan2go_enabled: false`, `certbot_extra_sans: []`) since it sits *below* host_vars. See [roles/github_runner/defaults/main.yml](roles/github_runner/defaults/main.yml) for the canonical explanatory comment.
- **DO NOT run state-mutating commands on prod hosts (`lab`/`pug`/`bunk`) without explicit ack.** Diagnostic SSH is pre-authorized; mutations are not. See *Testing → Debugging prod hosts directly* for the boundary.
- **DO NOT use `_test.yml` for new role tests** — historical alias for `_setup.yml`; no roles still ship it.

## Project Structure & Module Organization

`site.yml` orchestrates host groups → roles. `ansible.cfg` binds the repo to `hosts.ini` and `vault-client.sh` (which dispatches on vault-id; see *Vault ids* below), so running from the root picks up the correct inventory and vault passwords automatically.

| Path             | Purpose                                                                                      |
|------------------|----------------------------------------------------------------------------------------------|
| `roles/<r>/`     | role logic — `tasks/`, `templates/`, `files/`, `vars/` (no `handlers/`, per Hard Rules)      |
| `group_vars/`    | shared defaults — directory split (`group_vars/all/main.yml` etc., not a single file)        |
| `host_vars/`     | host-specific overrides                                                                      |
| `terraform/`     | DNS (Cloudflare), registrar (Gandi), Cloudflare Access, Mailgun, GitHub-repo, Nexus, GCP     |
| `packer/`        | QEMU image builds                                                                            |
| `test/`          | harness, inventories, logs                                                                   |
| `wireguard/`     | VPN peers (vaulted keys)                                                                     |
| `notes/`         | long-form design notes — the landing zone for content that doesn't belong inline             |

## Build, Test, and Development Commands

- Bootstrap: install [mise](https://mise.jdx.dev), `mise trust`, then `mise install` from the repo root — pins terraform, packer, python, uv, and shellcheck. `python.uv_venv_auto` auto-creates and sources `.venv`, so `uv sync` populates Python deps (ansible, ansible-lint, black, yamllint) on first directory entry. 1Password CLI must be signed in for the `op://` env vars in `mise.toml` to resolve.
- Configure everything: `mise run ansible -- --limit prod` (the wrapper handles vault-id, ssh args, and env; `ansible.cfg` already points at `hosts.ini`; set `--tags` to narrow scope).
- Focus on one service or host: `mise run ansible -- -l lab --tags wireguard`.
- Manage DNS: `mise run tf init`, `mise run tf plan`, then `mise run tf apply` — the `tf` task `cd`s into `terraform/` and forwards everything to `tofu` (use `--` for flags mise might intercept, e.g. `mise run tf plan -- -refresh=false`). State lives in MinIO (`s3://terraform/homelab.tfstate` on `minio-api.lab.fahm.fr`), AES-GCM-encrypted client-side from `TF_VAR_state_passphrase` (1Password via mise `[env]`). Rotation procedure: [notes/terraform-state-encryption-rotation.md](notes/terraform-state-encryption-rotation.md).
- Refresh the integration image when the base OS changes: `mise run packer:build` builds every source defined in `packer/qemu.pkr.hcl` in parallel. Pass names to narrow: `mise run packer:build box` or `mise run packer:build pug lab`. `--ubuntu noble` targets a different release. Three sources (`box` / `pug` / `lab`) — see [notes/test_environment_design.md](notes/test_environment_design.md).
- Lint everything: `mise run lint` runs `ansible-lint`, `tofu fmt -check`, `tflint`, `packer fmt -check` + `packer validate`, `black` / `ruff`, `yamllint`, `shellcheck` + `shfmt`, `typos`, and a topology JSON-schema check in parallel; `mise run fmt` applies fixes (notably `mise run fmt:ansible` = `ansible-lint --fix` for autofixable rules like `yaml[truthy]` / `name[casing]` / `no-free-form` — prefer this over hand-editing).
- Inner-loop ansible-lint: prefer `mise run lint:ansible-changed` (~4s; lints YAML changed vs `origin/master` + uncommitted + untracked) over the full `mise run lint:ansible` (~40s, dominated by per-role `ansible-playbook --syntax-check` subprocesses). Override the base ref with `LINT_BASE=<ref>`. Run full `mise run lint` before pushing.
- Resume mid-converge: when a role fails partway through under `testrole.py --keep`, re-run with `mise run ansible -- -l <host> --start-at-task '<failing task name>'` instead of re-running the whole role from scratch. `--step` walks task-by-task with y/n prompts when bisecting a regression.

## Workflows — use the skill, don't reinvent

Recurring multi-step workflows live as skills. Reach for them before hand-rolling the equivalent:

- `/triage <service>` — investigate a homelab service end-to-end (resolve host(s), gather state, summarize).
- `/verify` — validate a code change by running the app and observing behavior.
- `/review` — review a pull request.
- `/commit-push-pr` — commit, push, open a PR.
- `/split_worktree_commits` — split a multi-finding worktree into per-finding commits.
- `/new_podman_role` — scaffold a new podman service role following the conventions in *Podman Service Conventions* below.

## Coding Style & Naming Conventions

**Underscores, not hyphens** in identifiers we author: role names, file names under `roles/`, systemd unit names, vars, directory names under `/mnt/services/<svc>/`, etc. — `clickhouse_logs` not `clickhouse-logs`, `service_user.yml` not `service-user.yml`. Editor double-click on `foo_bar` selects the whole token; on `foo-bar` it stops at the hyphen. Exceptions are names dictated by upstream (image tags, package names, K8s-flavored YAML keys) — those keep their hyphens.

Everything else (YAML indentation, descriptive `name:`, `set -euo pipefail` in bash, namespaced role vars) is enforced by `mise run lint`; don't lift those into commentary here.

## Repo Conventions

### Role conventions

**Load-bearing idioms** — these break things silently if missed:

- Use `qemu_test` to gate test-only branches: set in test-fixture host_vars (`host_vars/box.yml`, `host_vars/minimal.yml`) and in `host_vars/lab-qemu.yml` / `host_vars/pug-qemu.yml` overlays the harness applies on top of inventory-loaded `host_vars/{lab,pug}.yml` when `--machine lab/pug` is used. Use `qemu_test` in `when:` / `ternary` rather than per-machine name comparisons. `minimal` additionally sets `qemu_test_minimal: true`.
- Per-role test hooks live alongside the role's tasks:
  - `tasks/_setup.yml` — pre-role fixture bringup (e.g. start a local gitea instance for the role to register against). **Never runs against prod** — only invoked from the test harness.
  - `tasks/_verify.yml` — post-converge assertions exercising the role's real behavior: hit the service over its port, trigger the failure path. **Only ever invoked by `test/testrole.py` / `test/testall.py` against a qemu VM** — `site.yml` and `mise run ansible --tags <role>` against prod never run it. That contract lets `_verify` scaffolding (netns/veth fixtures, sentinel tables, test-only listeners) sit alongside the role's real tasks without an explicit `when: qemu_test` gate; if you ever need `_verify` to run on prod, gate the destructive bits first. Stat-only ("file present", "unit active") checks drift toward tautology — fall back to them only when functional testing is impossible, and say why in a comment.
- Roles whose own name starts with `_` (currently `roles/_test/`, `roles/_packer/`) are **test-fixture roles**, not service roles — they bundle assertions or scaffolding invoked by the harness (not by `site.yml`). Don't confuse with the `_test.yml` Hard Rule above, which forbids that filename inside a *service* role. New service roles never get a `_` prefix.
- Tasks whose target depends on a freshly-created user, group, or directory must be gated with `when: not (ansible_check_mode and <svc>_user.changed)`. In check mode the prerequisite task only *reports* it created the user/dir — nothing is actually written — so a downstream `file:` / `copy:` / `template:` will fail with "no such user" or "no such directory". Every service role that creates a system user before writing files relies on it.
- Prefer `import_role` / `import_tasks` over `include_role` / `include_tasks`. Static forms parse-check at lint time, dedupe identical calls, and don't suffer the `loop:` + `vars:` "vars bind once" bug (a task-level `vars:` block on an `include_role: loop:` is evaluated once at parse time, not per iteration). Fall back to `include_*` only when name or vars are genuinely dynamic, and then wrap the loop body in a per-iteration `include_tasks: <inner>.yml` so each iteration gets a fresh scope.
- For tasks that mutate state but should run only once (downloads, registrations, token fetches), gate with `args: creates: <sentinel>` rather than `changed_when: false`. Skipped tasks don't count against the harness's idempotence check; `changed_when: false` lies about what the task actually does.

**Style:**

- Centralize downloadable artifact metadata in `roles/<role>/vars/main.yml`. Keep the full URL **adjacent to its sha256** keyed by `ansible_architecture` (`x86_64` / `aarch64`), so a human auditing a checksum can see exactly which URL it pins without recomposing it from a version constant.
- Bare module names (`command:`, `apt:`, `deb822_repository:`, `assert:`) are the default for `ansible.builtin.*`; FQCN is only for modules from other collections (`containers.podman.podman_network`, `community.general.timezone`, etc.). Don't mix the two styles within a single file.
- Every `copy:` / `template:` writing a config file should carry a best-effort `validate:` that *parses* (not just reads) the rendered file — catches jinja quoting bugs and vault-substitution typos before the broken file lands on disk (many services accept-then-overwrite-with-defaults on parse failure, silently losing config). Pick the parser matching the format: `python3 -c 'import yaml,sys; yaml.safe_load(open(sys.argv[1]))' "%s"` for YAML, `python3 -c 'import configparser; configparser.ConfigParser(allow_no_value=True).read("%s")'` for INI, `nginx -t -c %s` for nginx, `visudo -cf %s` for sudoers. These strings are safe as `validate:` arguments (ansible passes them to `%s` substitution, not through shell pre-exec); don't lift them verbatim into `command:` / `shell:` blocks — the embedded single quotes will break task loading. If no syntax-checker exists, omit `validate:` rather than invent a fake one.
- `/mnt/services/<svc>/` is for service *state* (configs, databases, secrets, persistent data — anything that must ride ZFS snapshots/replication). Service *code* shipped from the repo (`roles/*/files/*.py`, scripts deployed via `copy:`) belongs at `/opt/<svc>/` — reproducible from the role, doesn't need snapshotting. [roles/homepage/tasks/main.yml](roles/homepage/tasks/main.yml) is the canonical split: YAML configs under `/mnt/services/homepage/`, Python sidecar binary under `/opt/homepage_alerts/`.

### Service ports

Ports each host-level service publishes live in `group_vars/all/main.yml` under `service_ports:` — single source of truth for unit templates, nginx `proxy_pass`, firewall rules, and cross-role consumers (e.g. pihole's dnscrypt upstream). Covers both loopback-published podman services (`--publish 127.0.0.1:{{ service_ports.<svc> }}:<container_port>/tcp` + `nginx_site_args.proxy_pass: http://localhost:{{ service_ports.<svc> }}/`) and native daemons whose port is referenced by the firewall or another role. Migrate roles to consume from there as you touch them — see [notes/SOMEDAY.md](notes/SOMEDAY.md) *Migrate remaining service roles to the `service_ports` registry*. **When allocating a new port, grep both `service_ports:` and `127.0.0.1:` literals in `roles/*/templates/*.j2` first** — un-migrated roles still hard-code their port, so the registry-only check returns false negatives.

### ZFS site mountpoints

Per-site dataset gates live in `group_vars/all/main.yml` under `zfs_has_<name>_mount:` (services / scratch / media / data / brumath / eckwersheim / minio). Producer/consumer split: **producers** (`roles/data`, `roles/media`, `roles/scratch`, `roles/minio`, `roles/services`, `roles/libvirt`) create the dataset unconditionally on every `zfs_root` host they run on and **never read the flag**. **Consumers** (service unit templates and samba's mount overrides) gate on `zfs_has_<name>_mount` to decide whether to bind-mount `/mnt/<name>`. Test fixtures (`host_vars/box.yml` + `host_vars/{lab,pug}-qemu.yml` overlays) flip every flag to `false`, so the qemu VMs exercise consumer roles cleanly without prod-data dependencies while producer roles still get exercised on the test VM. Adding a new site mount: register the flag in `group_vars/all/main.yml` (defaulting `true`), add a producer task, and gate `BindReadOnlyPaths=/mnt/<name>` in every consuming `.service.j2`. Don't gate the producer — it would silently turn the role into a no-op under the default test machine.

### Homepage bookmarks

Any new user-facing service (anything with an nginx subdomain) gets a bookmark in [roles/homepage/templates/bookmarks.yaml.j2](roles/homepage/templates/bookmarks.yaml.j2) — pick the right section (Media / Infra / Personal), follow the existing `abbr: XX` + `icon: sh-<name>.png` + `href: https://<subdomain>.{{ inventory_hostname }}.{{ domain }}/` shape. Icons resolve through selfh.st (the `sh-` prefix); if no selfh.st icon exists, omit the `icon:` line and the 2-letter `abbr:` becomes the visible fallback.

### Helper roles

Prefer these over re-implementing the boilerplate; a hand-rolled equivalent in a service role is a migration candidate.

- **`service_user`** — `import_role: name: service_user, tasks_from: user` with `service_user_args: { name: <svc> }` creates the standard system group + nologin user (`/nonexistent` home) and `/mnt/services/<svc>` config dir, and exposes `<svc>_user.uid` / `.group` to the caller. Optional dict keys: `extra_groups` (e.g. `[kvm]`), `createhome: true`, `home`, `shell`, `config_dir: false`. Inputs flow through the dict (not individual `service_user_X` vars) so each call replaces the dict wholesale and inter-call vars-leak can't happen.
- **`podman_secret`** — `import_role: name: podman_secret, tasks_from: secret` with `podman_secret_args: { name: <n>, data: <d> }` creates or rotates a podman secret. Optional `user` key (default `root`; non-root targets a specific rootless podman). Tracks the data sha256 as a label on the secret itself and force-recreates only when content drifts (the upstream module is name-idempotent only). Exposes `<name>_rotated` and `<name>_existed_before` so callers can wire restart triggers and detect post-bootstrap rotations of state that consumes the secret only at first creation (e.g. Django superuser via `SUPERUSER_PASSWORD` — fail loudly and tell the operator to run `manage.py changepassword` rather than rotate silently).
- **`systemd_unit`** — `import_role: name: systemd_unit, tasks_from: {install,dropin,service}` installs/validates a unit, reloads systemd, and (for `service`) pre-pulls referenced podman images and starts/restarts the unit. Inputs flow through a single dict `systemd_unit_args` (keys: `src`, `dest`, `user`, `condition`, `start`, `restart`, `reload`, `verify`, `for`). The dict form is load-bearing: with the old individual-var form, `import_role: vars:` leaks values as play-scoped vars and a previous caller's value would bleed into the next caller. `install` dispatches to copy or template based on `src`'s extension (`foo.service.j2` → template, `foo.service` → copy). `dropin` writes `<scope>/<unit>.d/override.conf` for a unit shipped elsewhere; pass `for: <unit>`. Drive restarts via `restart: "{{ <unit-dest>_result.changed or <other>_rotated }}"` so config / secret changes propagate. Always prefer this helper over a hand-written `template:` + `systemd:` pair.
- **`systemd_timer`** — `import_role: name: systemd_timer, tasks_from: {install,remove}` with `systemd_timer_args: { name, exec_start, on_calendar, frequency }` installs a paired `<name>.service` + `<name>.timer` (system scope by default). Optional keys: `description`, `randomized_delay_sec`, `timeout_start_sec`/`timeout_stop_sec`, `monitor` (emits the netdata `systemd_timers` collector metadata for cadence-overdue detection), `user` (non-root needs linger), `run_as` (drops privileges in a system-scope unit — preferred over user-scope; see [roles/getmail/tasks/main.yml](roles/getmail/tasks/main.yml)). Use this for any cron-shaped job.
- **`nginx_site`** — `import_role: name: nginx, tasks_from: site` with `nginx_site_args: { subdomain, proxy_pass, ... }` installs an `<subdomain>.<host>.<domain>` vhost with TLS, HSTS, CSP, and (optional) Authelia auth. Optional keys: `state: absent` to retire a site (keep the call as a tombstone), `proxy_buffering`, `client_max_body_size`, `enable_http`, `csp_*`, `auth: required` for Authelia-gated sites. See [roles/nginx/tasks/site.yml](roles/nginx/tasks/site.yml) for the full key list. Every user-facing service uses this — never hand-roll an nginx vhost.
- **`macvlan`** — list-driven per-host setup of macvlan ifaces and (optionally) matching podman networks via `macvlan_blocks:` in host_vars. Per-entry schema is documented at the top of [roles/macvlan/tasks/main.yml](roles/macvlan/tasks/main.yml). **IP space layout** within a block: lower /29 = host iface + libvirt VMs, upper /29 = podman containers (via netavark ip_range). Treat the ip_range as a flat allocation pool — every IP in the cidr is a usable container slot; the real gateway is on the parent VLAN, not synthesized inside the range. Consumers attach via `--network={{ name }}` — see `roles/homeassistant` for an example.

## Podman Service Conventions

Long-form rationale (gotchas, history, why-not-skip) in [notes/podman_conventions.md](notes/podman_conventions.md). For new roles use `/new_podman_role`. Consult notes when an edge case doesn't fit.

### Healthchecks

Every `*.service.j2` declares `--health-cmd` (and `--health-startup-cmd`); the check runs *inside* the container. Preference order:

1. `curl` / `wget` already in the image — grep the Dockerfile first.
2. Python `urllib.request` with the URL in argv (python-based images). Use the **JSON-array form** — the naïve `'u.urlopen("...")'` shape unescapes mid systemd→podman handoff; see notes for the incantation.
3. Service-native CLI — `redis-cli ping`, `mosquitto_sub`, `dig +short @127.0.0.1`.
4. `static_curl` role escape hatch — for distroless images. No current consumers; see [notes/podman_conventions.md](notes/podman_conventions.md) tier 4.

### Secrets

Three paths to inject a podman secret, preferred order:

1. **App-native `*_FILE`.** `--secret=<n>,type=mount,target=<basename>` + `--env XXX_FILE=/run/secrets/<basename>`. Secret never lands in env. Canonical: `EMAIL_HOST_PASSWORD_FILE` in [roles/healthchecks/templates/healthchecks.service.j2](roles/healthchecks/templates/healthchecks.service.j2).
2. **linuxserver `FILE__<VARNAME>`** prefix — s6-overlay reads the file at startup. linuxserver-specific.
3. **`type=env,target=VAR`** — last-resort, visible to `podman inspect` and `/proc/<pid>/environ`. Acceptable on single-operator hosts; prefer 1 or 2.

### User namespacing

Three-tier pick — simplest that works. **Note:** the repo grew up under a "uidmap everywhere" assumption that's since been inverted — most existing units use tier 2 or 3, but tier 1 is the target end-state for new and migrated roles. See [notes/SOMEDAY.md](notes/SOMEDAY.md) *Audit per-service container-user choice*.

1. **`--user {{ <svc>_user.uid }}:{{ <svc>_user.group }}`** (default). No namespace mapping; bind-mount ownership is trivially correct. Use whenever the app doesn't insist on `id -u == 0`.
2. **linuxserver `PUID` / `PGID`** — image's s6-overlay aligns its baked-in `abc` user to the supplied ids at startup. linuxserver-specific.
3. **Fake-root uidmap** — last resort when the image insists on `id -u == 0`: `--user 0:0` + `--uidmap=0:0:65536 --uidmap=+0:{{ <svc>_user.uid }}:1` (and gidmap pair). Canonical: [roles/healthchecks/](roles/healthchecks/). When the image's entrypoint then drops to a baked-in non-root uid mid-startup (ClickHouse → 101, MongoDB → 100), allocate a dedicated host user via `service_user` (`config_dir: false`), add a `+N:<uid>:1` override, and assert ownership in `_verify.yml`. Canonical: [roles/hyperdx/](roles/hyperdx/).

### Prefer system-scope systemd units

Default for new timers/services is **system-scope** (`/etc/systemd/system/`, `systemd[1]`). Reach for user-scope (`systemctl --user`, linger) only when fundamentally required — today that's just rootless podman ([roles/gitea_runner/](roles/gitea_runner/), [roles/github_runner/](roles/github_runner/)). One operational vocabulary (`systemctl status`, `journalctl -u` without `--user --machine=<u>@.host`); netdata's `systemdunits` collector attaches to the system bus only; single privilege-drop model (`User=` / `Group=`, or `systemd_timer`'s `run_as:` — [roles/getmail/tasks/main.yml](roles/getmail/tasks/main.yml) is the canonical example).

When hardening a unit, `systemd-analyze security <unit>` scores it against ~50 sandboxing directives (`NoNewPrivileges`, `ProtectSystem`, `PrivateTmp`, `RestrictNamespaces`, …) and lists which would be cheap wins. Run it against any new `.service.j2` you author. (Syntax validation — `systemd-analyze verify` — is already automated by the `systemd_unit` helper's `verify:` arg; you don't have to run it by hand.)

## Testing Guidelines

The harness lives in `test/` (Python, asyncio-based).

### Harness CLI

`test/testrole.py <role>` boots `box` (default) and applies the role end-to-end. `test/testall.py` fans out role × machine in parallel. `--help` on either has the full flag list; the load-bearing ones are `--machine {minimal,box,lab,pug}`, `--keep` (leave the VM up for SSH debug), and `test/testall.py --retry-failed` (rerun rows from `test/out.tsv`).

Exit codes: `0` success, `1` converge failure, `124` per-test timeout, `125` idempotence failure, `130` user-cancelled. The joblog records the integer so you can sort/filter by failure mode.

Output: colorized, written to `test/out/<machine>.<role>.ansi`; on failure the systemd journal is collected and the last 50 lines tailed to stdout. Keep artifact trees under `/mnt/scratch/qemu/<codename>/` (Linux) or `packer/artifacts/<codename>/` (Mac). macOS needs `xorriso` for the cloud-init seed iso. Include `mise run ansible -- --check` / `terraform plan` snippets in reviews so idempotence and drift are obvious.

### Debugging prod hosts directly

SSH to `lab` / `pug` / `bunk` works out of the box (see [hosts.ini](hosts.ini); key + sudoers already configured). For investigation — service logs, daemon state, hardware probes, on-disk config — connect and run the diagnostics yourself rather than asking the user to paste output (e.g. `ssh lab 'sudo fan2go detect'`, `ssh pug 'systemctl list-units --failed'`).

**Authorization boundary.** Diagnostic reads on read-only state are pre-authorized. The following require explicit ack each time (per *Hard Rules*):

- Anything mutating: `systemctl restart` / `reset-failed`, `nft -f`, `apt` / `dnf` (even `list` — network egress), editing config files, anything under `/mnt/services/*/secrets/` or `/etc/credstore*`.
- Anything that *exfiltrates secrets into the chat transcript*: `journalctl -u <svc>` for services that log credentials (`postfix`, `getmail`, `*_runner`, `healthchecks` mail path), `podman secret inspect`, reading vault files, env-style dumps from running services.

When in doubt, ask.

### Test environment design

Variant table (axis-collapse rationale + ZBM-aarch64 workaround in [notes/test_environment_design.md](notes/test_environment_design.md)):

| Variant   | Disks                                                       | Use case                                                      |
|-----------|-------------------------------------------------------------|---------------------------------------------------------------|
| `minimal` | Vanilla cloud image, ext4                                   | Stranger-baseline; in `ci-minimal-roles.txt` only             |
| `box`     | Single-disk rpool, no extra pools                           | Default push-CI fixture; producer roles run flat under rpool/ |
| `pug`     | 1 rpool + 2 apoc                                            | Matches pug prod host; on-demand + nightly                    |
| `lab`     | mdadm-EFI + 3-disk mirror rpool + dozer/tank(raidz2)/mouse  | Matches lab prod host; on-demand + nightly                    |

Push CI fans out only to `box` (and `minimal` for roles in [.github/ci-minimal-roles.txt](.github/ci-minimal-roles.txt)). Nightly runs the full universe across all variants. Editing pool topology requires a packer rebuild (15-30 min) — the post-boot `test/disks/<variant>.sh` mechanism is gone.

## Continuous Integration

GitHub Actions on lab via the `github_runner` role (templated `github_runner@<repo>_<suffix>.service`, one process per entry in [host_vars/lab.yml](host_vars/lab.yml)'s `github_runner_instances`). Internals — per-instance dirs, registration-token mint, KVM-gidmap brittleness — in [notes/github_runner_design.md](notes/github_runner_design.md).

Five workflows in [.github/workflows/](.github/workflows/):

- `lint` — every push; runs `mise run lint` in the ci container.
- `test` — every push; `detect` fans out per-role via `mise run ci:detect-roles`. Cross-cut paths (`group_vars/all/main.yml`, `host_vars/{box,minimal}.yml`, `test/(testrole|testall|machine).py`, `mise.toml`, etc.) emit an empty matrix and mail manual-trigger instructions.
- `test-nightly` — `0 2 * * *` + dispatch; full-universe matrix, mails on failure. Activity gate skips when no commits landed in 25h.
- `packer-build` — dispatch + push touching `packer/**`; only writer of `/mnt/scratch/qemu`. Shares the `lab-qemu-artifacts` concurrency group with `test`/`test-nightly`; reader/writer flock details in [notes/concurrency_rework.md](notes/concurrency_rework.md).
- `ci-image` — dispatch + **push to `master`** touching Dockerfile inputs; rebuilds `nexus.lab.fahm.fr/homelab/ci:<sha>` + `:latest`. Runs *outside* the ci container; bootstrap a fresh runner by dispatching this once before other workflows can succeed.

**Variant escalation** — [.github/ci-minimal-roles.txt](.github/ci-minimal-roles.txt) gives a role an extra `(role, minimal)` matrix entry. Add only when behaviour depends on upstream-shipped packages (every entry doubles the role's cost).

**Runner pools** — VM workloads on `lab-vm`, everything else on `lab`; pools are disjoint. Per-job `runs-on:` lives in each workflow file.

**CI secrets** are terraform-managed end-to-end; rotation in [notes/ci_secrets_runbook.md](notes/ci_secrets_runbook.md). The terraform GitHub provider authenticates via the operator's `gh auth token` (never exposed to CI).

The `gitea_runner` role still ships a Gitea-side runner on lab for ad-hoc workflows; this repo's CI doesn't use it.

### Local-debug recipes

- Manual subset: Actions → `test` → Run workflow → `roles=foo,bar:lab,baz:minimal` (`roles=ALL` runs everything; bare `foo` becomes `foo:box` plus `foo:minimal` if listed).
- `CI_BASE_REF=HEAD~5 mise run ci:detect-roles` — preview the matrix for a multi-commit push.
- `GITHUB_EVENT_NAME=workflow_dispatch INPUTS_ROLES=foo,bar mise run ci:detect-roles` — preview a manual dispatch.
- `mise run ci:role-deps <helper>` — list consumers of a helper role.

## Commit & Pull Request Guidelines

History favors short, imperative subjects such as "Fix dnscrypt" or "Add profilarr"; prefix with a role when it helps clarity (`wireguard: rotate peers`). Each PR should summarize the motivation, list impacted hosts or roles, and link related issues. Mention which commands were run (`test/testrole.py`, `test/testall.py`, `terraform plan`, screenshots when relevant) and flag inventory, vault, or DNS updates so reviewers can re-run `vault-client.sh`.

**Splitting a multi-change worktree into per-finding commits.** Use `/split_worktree_commits` for the standard flow. Manual recipe: drive `git add --patch=<file>` non-interactively with `printf 'y\nn\nn\n' | git add -p <file>` (y = stage, n = skip; answer `s` first to split hunks). **Always `git diff --staged` and visually verify before `git commit`.** Do **not** revert the worktree to redo each edit from scratch — wastes effort, loses the just-tested state.

**Never stage a vault-rendering template hunk-by-hunk.** Files containing rendered secrets (`templates/*.j2` referencing `*_password` / `*_token` / `*_secret`) must be staged whole-file or not at all — a misordered hunk can silently mix old and new secret states, and the `printf 'y\nn\nn\n'` pattern bypasses interactive confirmation that would otherwise catch it.

## Security & Configuration Tips

Do not commit decrypted data; access secrets through `ansible-vault edit <path>` (or the equivalent), and keep them in `group_vars/*.yml` / `host_vars/*.yml`. WireGuard keys in `wireguard/` must remain vaulted and rotate with peer changes. When touching networking or DNS, run playbooks with `--limit` and apply Terraform only after review in a dedicated branch.

This is a home environment, not a company production site: some security is nice and worth learning about, but we don't overcomplicate things.

### Vault ids: `prod` vs `test`

Two passwords, two scopes. Configured in [ansible.cfg](ansible.cfg) as `vault_identity_list = prod@vault-client.sh, test@vault-client.sh` with `vault_id_match = True`.

- `prod` — encrypts `group_vars/prod.yml` and prod host_vars (lab, pug, bunk). Stays on local workstations only; never lands in CI.
- `test` — encrypts `group_vars/test.yml` and test host_vars (currently just `host_vars/box.yml`). Available to CI as the **GitHub** repo secret `HOMELAB_VAULT_PASSWORD_TEST` so the test harness can decrypt test vars. Anything encrypted with the `test` vault id is reachable to any CI workflow run — never put a credential there that has prod blast radius.

[vault-client.sh](vault-client.sh) follows ansible's "client" password-script protocol (filename must end in `-client` for ansible to pass `--vault-id <id>`). Lookup order per id: env var `HOMELAB_VAULT_PASSWORD_<UPPER_ID>` first (CI only), then macOS keychain `homelab-vault-<id>`, then Linux file `~/.config/homelab/vault-pass-<id>` (mode 0400). Bootstrap recipes in [notes/vault_setup.md](notes/vault_setup.md).

When encrypting a new value with `ansible-vault encrypt_string`, pass `--encrypt-vault-id prod` (or `test`) so the resulting envelope carries the right label — otherwise `vault_id_match = True` will refuse to decrypt it.

To re-label an already-encrypted file between vault ids (promote `test` → `prod` once a value's blast radius grows, or vice versa), run `ansible-vault rekey --new-vault-id <new>@vault-client.sh <path>`. The envelope is rewritten in place, single-file diff.

## Someday

Open follow-ups deferred from prior reviews live in [notes/SOMEDAY.md](notes/SOMEDAY.md). Items there are backlog notes, not standing instructions — don't enact one without explicit operator confirmation.
