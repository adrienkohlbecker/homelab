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
- **DO NOT default required service inputs in `vars/main.yml`** — role vars sit *above* host_vars in ansible's precedence ladder and silently mask host-level overrides. Required inputs live in `host_vars`/`group_vars` and the role must `assert:` they're set. `defaults/main.yml` is fine for optional host-overridable values since it sits *below* host_vars. Canonical: [roles/github_runner/defaults/main.yml](roles/github_runner/defaults/main.yml).
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
- **op:// env refs only resolve under `op run --`.** Toml tasks wrap explicitly; file-based tasks under `mise-tasks/` do **not** — mise exports the literal `op://…` string. Fix: re-exec under `op run --` behind a guard env var (preamble at top of [mise-tasks/ha/sync.py](mise-tasks/ha/sync.py)).
- Configure everything: `mise run ansible --limit prod` (wrapper handles vault-id, ssh args, env; set `--tags` to narrow scope). One service/host: `mise run ansible --limit lab --tags wireguard`.
- DNS/terraform: `mise run tf {init,plan,apply}` — `cd`s into `terraform/` and forwards to `tofu` (use `--` for flags mise intercepts). State in MinIO (`s3://terraform/homelab.tfstate`), AES-GCM-encrypted. Rotation: [notes/terraform-state-encryption-rotation.md](notes/terraform-state-encryption-rotation.md).
- Refresh integration image: `mise run packer:build [box|pug|lab]` (parallel; `--ubuntu noble` for another release). See [notes/test_environment_design.md](notes/test_environment_design.md).
- Lint: `mise run lint` (ansible-lint, tofu/packer fmt+validate, tflint, black/ruff, yamllint, shellcheck+shfmt, stylua+selene, taplo, markdownlint — all parallel); `mise run fmt` applies fixes (`fmt:ansible` = `ansible-lint --fix` — prefer over hand-editing). Inner-loop: prefer `mise run lint:ansible-changed` (~4s; override base via `LINT_BASE=<ref>`) over full `lint:ansible` (~40s). Run full `mise run lint` before pushing.
- Resume mid-converge: after a `testrole.py --keep` failure, `mise run ansible --limit <host> --start-at-task '<failing task name>'`. `--step` walks task-by-task when bisecting.

## Workflows — use the skill, don't reinvent

- `/triage <service>` — investigate a service end-to-end (resolve host(s), gather state, summarize).
- `/split_worktree_commits` — split a multi-finding worktree into per-finding commits.
- `/new_podman_role` — scaffold a new podman service role per *Podman Service Conventions*.

## Coding Style & Naming Conventions

**Underscores, not hyphens** in identifiers we author: role names, files under `roles/`, systemd units, vars, dirs under `/mnt/services/<svc>/`. Exceptions: names dictated by upstream. Everything else enforced by `mise run lint`.

**Never set `no_log: true`.** Applies run interactively on the operator's workstation, never captured to a file or CI log — hiding the diff only makes failures harder to debug.

**Every bash script starts `set -euo pipefail`** — proper files *and* inline ansible `shell:` blocks (which must also declare `executable: /bin/bash`). Enforced by the custom **`shell-strict-mode`** rule ([lint/ansible_rules/shell_strict_mode.py](lint/ansible_rules/shell_strict_mode.py)); test scaffolding (`_verify*`/`_setup*`) exempt. Handle expected-failure commands with `|| true` and avoid `… | head` pipelines (SIGPIPE) — collapse into `awk`. **No apostrophes inside `shell:`/`command:` block scalars** — even in `#` comments within the block; ansible's pre-exec shlex pass treats `'` as an opening quote and fails task loading. YAML-level comments (outside the scalar) are fine.

**Comments describe current state, not history.** No "this used to…", "was removed", "replaces the old…". That context belongs in the commit message. Explaining why the current code *deliberately* avoids an obvious alternative is fine.

## Repo Conventions

### Role layering (`site.yml`)

`site.yml` is ordered as a **layer ladder** — a role's converge position *is* its layer, and each layer builds on the guarantees of the ones above. Two bands:

- **Base machine install** (`hosts: box,lab,pug,fox`) — sub-bands: *host base* (OS, networking, access), *storage & boot* (`zfs`/`zfs_mount`/`zfs_autobackup`/`zfsbootmenu`/`refind`), *service platform* (`podman`, `services`, `certbot`, `nginx`), *observability* (`csplogger`/`netdata`/`fluentbit`). Roles within host-base are mutually independent.
- **Services** — host-scoped plays that assume the full platform is already in place.

**Where does a new role go?** Stop at the first layer whose guarantees you need: booted OS → host base; ZFS → after storage; podman/nginx-TLS → after platform (the base⇄service watershed); app for subset of hosts → service play. Keep dataset *producers* ahead of consumers.

### Role conventions

**Load-bearing idioms** — these break silently if missed:

- Gate test-only branches on `qemu_test` (set in `host_vars/{box,minimal}.yml` and `host_vars/{lab,pug}-qemu.yml` overlays). `minimal` also sets `qemu_test_minimal: true`.
- Per-role test hooks live alongside the role's tasks:
  - `tasks/_setup.yml` — pre-role fixture bringup. **Never runs against prod.**
  - `tasks/_verify.yml` — post-converge assertions. **Only invoked by the harness against a qemu VM** — never against prod. That contract lets scaffolding sit alongside real tasks without a `when: qemu_test` gate. Stat-only checks drift toward tautology — fall back only when functional testing is impossible. Rebooting inside `_verify` is fine for next-boot state (canonical [roles/console/tasks/_verify.yml](roles/console/tasks/_verify.yml)).
- Tasks depending on a freshly-created user/group/dir must be gated `when: not (ansible_check_mode and <svc>_user.changed)` — in check mode the prerequisite only *reports* creation.
- Prefer `import_role`/`import_tasks` over `include_*`. Fall back to `include_*` only for genuinely dynamic name/vars, then wrap the loop body in a per-iteration `include_tasks` for fresh scope.
- For state-mutating tasks that should run once, gate with `args: creates: <sentinel>` not `changed_when: false`.

**Style:**

- Centralize downloadable artifact metadata in `roles/<role>/vars/main.yml`: full URL **adjacent to its sha256**, keyed by `ansible_architecture` (`x86_64`/`aarch64`).
- Every config-writing `copy:`/`template:` carries a best-effort `validate:` that parses the rendered file. Omit rather than invent a fake one. Safe as `validate:` args but **don't** lift into `command:`/`shell:` — embedded quotes break task loading.
- **Preserve upstream comments in vendored config files.** Deliberate exception to "comments describe current state": the upstream reference *is* current state for a config file.
- **Don't pin a config value just because it equals the upstream default.** Pin only when load-bearing: documents a dependency, is a deliberate non-default, or is a tuning knob worth surfacing.
- Every file-writing task sets **`backup: true`** — enforced by the `require-backup` rule ([lint/ansible_rules/require_backup.py](lint/ansible_rules/require_backup.py)); test scaffolding and `_`-roles exempt. Exceptions carry `# noqa: require-backup`.
- `/mnt/services/<svc>/` is service *state* (configs, DBs, secrets — rides ZFS snapshots). Service *code* from the repo belongs at `/opt/<svc>/`. Canonical: [roles/homepage/tasks/main.yml](roles/homepage/tasks/main.yml).

### Service ports

Ports live in `group_vars/all/main.yml` under `service_ports:` — single source of truth. **Scope: only operator-reachable ports** (host-published `--publish` or loopback binds). Container-to-container traffic over podman networks stays as inline literals. **When allocating a new port, grep both `service_ports:` and `127.0.0.1:` literals in `roles/*/templates/*.j2`** — un-migrated roles still hard-code their port.

### ZFS site mountpoints

Per-site dataset gates in `group_vars/all/main.yml` under `zfs_has_<name>_mount:` (services/scratch/media/data/brumath/eckwersheim/minio). **Producers** create datasets unconditionally and **never read the flag**. **Consumers** gate on the flag for bind-mounts. Test fixtures flip every flag `false`. Don't gate the producer — it would no-op under the default test machine.

### Homepage bookmarks

New user-facing services get a bookmark in [roles/homepage/templates/bookmarks.yaml.j2](roles/homepage/templates/bookmarks.yaml.j2) — follow the `abbr: XX` + `icon: sh-<name>.png` + `href: https://<subdomain>.{{ inventory_hostname }}.{{ domain }}/` shape. Icons resolve through selfh.st (`sh-` prefix).

### Home Assistant GUI YAML sync

Drive with `mise run ha:sync [pull|push|sync]` ([mise-tasks/ha/sync.py](mise-tasks/ha/sync.py)). Don't bypass the sync (no `scp`, no live-VM editing). **Don't bump the parent's submodule pointer**: [.gitmodules](.gitmodules) sets `ignore = all`, the pinned commit is not load-bearing.

### `notes/` submodule

`notes/` is a **git submodule** → private repo [adrienkohlbecker/homelab_notes](https://github.com/adrienkohlbecker/homelab_notes). **Not** a subtree — **never run `git subtree add/pull/push/merge`** against it. The parent stores only a gitlink; content never lands in the public repo.

Sync: edit inside `notes/` → commit+push to `homelab_notes` → record in parent with `git add notes && git commit`. **Unlike `ha_gui_config`, the `notes` pointer IS load-bearing** — divergence surfaces in parent `git status`; commit pointer bumps deliberately.

**In worktrees**, `worktree:populate`/`worktree:merge` ([mise-tasks/worktree/](mise-tasks/worktree/)) automate notes init and linear merging. Same-file conflicts halt for manual resolution.

**Every note must open with YAML frontmatter** containing at least `status` and `created_at`:

```yaml
---
status: current # optional inline note after #
created_at: 2026-05-20
---
```

Valid statuses: `runbook` (active operator procedures) · `current` (deployed state) · `planned` (near-term active work) · `deferred` (valid but not imminent) · `rejected` (decided against; kept for context) · `completed` (migration/investigation done; outcome in code/git) · `reference` (static lookup material). Archived notes live in `notes/archive/` and follow the same convention.

### Helper roles

Prefer these over re-implementing boilerplate. All take inputs through a single `*_args` dict (so each call replaces it wholesale and inter-call vars-leak can't happen). Full API: [notes/helper-roles-reference.md](notes/helper-roles-reference.md).

| Helper | Entry point | Exposes / creates |
|--------|-------------|-------------------|
| `service_user` | `tasks_from: user` | `<svc>_user.uid`/`.group`; `/mnt/services/<svc>` dir |
| `usergroup_immediate` | `tasks_from: user` | appends to group + `reset_connection` |
| `podman_secret` | `tasks_from: secret` | `<name>_rotated` / `<name>_existed_before` |
| `systemd_unit` | `tasks_from: {install,dropin,service,remove}` | `<unit>_started_result`; drives restarts inline |
| `systemd_timer` | `tasks_from: {install,remove}` | paired `.service`+`.timer` (system scope) |
| `nginx_site` | `import_role: name: nginx, tasks_from: site` | vhost with TLS/HSTS/CSP + optional Authelia |
| `sqlite_dataset` | `tasks_from: dataset` | `/mnt/services/sqlite/<name>/` + symlinked DBs |
| `macvlan` | driven by `macvlan_blocks:` in host_vars | ifaces + optional podman networks |

## Podman Service Conventions

Long-form rationale in [notes/podman_conventions.md](notes/podman_conventions.md). For new roles use `/new_podman_role`. **Use canonical upstream image names** in service templates (`docker.io/sonatype/nexus3:3.91.1`, not `nexus.lab.fahm.fr/docker.io/…`); mirror redirection belongs in `registries.conf` + the `--upstream-mirrors` test flag.

### Healthchecks

Every `*.service.j2` declares `--health-cmd` (and `--health-startup-cmd`). Preference order:

1. Service-native CLI — `redis-cli ping`, `dig +short @127.0.0.1`.
2. `curl`/`wget` already in the image — grep the Dockerfile first.
3. Python `urllib.request` (python images). **Must use JSON-array form** to survive systemd→podman handoff: `--health-cmd '["python","-c","import urllib.request as u, sys; u.urlopen(sys.argv[1], timeout=1)","http://localhost:PORT/"]'`.
4. `static_curl` — distroless images, last resort.

### Secrets

Three paths, preferred order:

1. **App-native `*_FILE`** — `--secret=<n>,type=mount,target=<basename>` + `--env XXX_FILE=/run/secrets/<basename>`. Never lands in env.
2. **linuxserver `FILE__<VARNAME>`** prefix — s6-overlay reads the file at startup.
3. **`type=env,target=VAR`** — last-resort, visible to `podman inspect`.

### User namespacing

1. **`--user {{ <svc>_user.uid }}:{{ <svc>_user.group }}`** (default). No namespace mapping. Use whenever the app doesn't insist on `id -u == 0`.
2. **linuxserver `PUID`/`PGID`** — s6-overlay aligns the baked-in `abc` user.
3. **Fake-root uidmap** — last resort: `--user 0:0` + `--uidmap=0:0:65536 --uidmap=+0:{{ <svc>_user.uid }}:1`. Canonical [roles/healthchecks/](roles/healthchecks/). When the entrypoint drops to a baked-in non-root uid, allocate a dedicated host user and add a `+N:<uid>:1` override (canonical [roles/hyperdx/](roles/hyperdx/)).

### Prefer system-scope systemd units

Default for new timers/services is **system-scope**. Reach for user-scope (linger) only when fundamentally required — today just rootless podman ([roles/gitea_runner/](roles/gitea_runner/), [roles/github_runner/](roles/github_runner/)). When hardening, `systemd-analyze security <unit>` lists cheap wins.

### Inter-container DNS

Containers reach co-located podman services via **`<name>.dns.podman`** (aardvark-dns), never a host port or hard-coded IP. Both producer and consumer create the `containers.podman.podman_network` independently (idempotent). Requires the **netavark** backend with `disable_dns: false`. Canonical: [roles/redis/](roles/redis) (producer) + [roles/z2m/](roles/z2m) (consumer). Target network name or gateway IP, never an `ethN` index.

## Testing Guidelines

The harness lives in `test/` (Python, asyncio).

### Harness CLI

`test/testrole.py <role>` boots `box` (default) and applies the role end-to-end. `test/testall.py` fans out role × machine in parallel. Flags: `--machine {minimal,box,lab,pug}`, `--keep`, `testall.py --retry-failed`. Exit codes: `0` success, `1` converge, `124` timeout, `125` idempotence, `130` cancelled. Output → `test/out/<machine>.<role>.ansi`. Artifact trees under `/mnt/scratch/qemu/<codename>/` (Linux) or `packer/artifacts/<codename>/` (Mac); macOS needs `xorriso` for the cloud-init seed iso.

**Flake policy:** every wait in the harness is **bounded** — a stuck boot surfaces as a quick failure, never a silent hang. Don't paper over flakes with auto-retry; fix the unbounded wait.

### Debugging prod hosts directly

SSH to `lab`/`pug`/`bunk` for diagnostics (pre-authorized). **Needs explicit ack:** anything mutating (`systemctl restart`, `apt`, config edits, `/mnt/services/*/secrets/`), anything exfiltrating secrets (`journalctl -u` for credential-logging services, `podman secret inspect`, vault files). **Bridge to ack:** run `mise run ansible --limit <host> --tags <role> --check` first so the operator sees the diff.

### Test environment design

Details in [notes/test_environment_design.md](notes/test_environment_design.md). **Packer images exist only for qemu test fixtures** — prod hosts are configured by ansible from stock Ubuntu. Variants: `minimal` (cloud ext4), `box` (single-disk rpool, default CI fixture), `box_deps` (box + pre-baked podman/nginx, opt-in via `machines: {box_deps:}` in `meta/test.yml`, saves ~140s/test), `pug`/`lab` (on-demand/nightly). `box_deps` rebuild: `mise run packer:seed-deps --ubuntu <codename>` after every `packer:build box`.

## Continuous Integration

GitHub Actions on lab via `github_runner`. Internals: [notes/github_runner_design.md](notes/github_runner_design.md). Workflows: `ci` (push/nightly/dispatch; `detect` fans out per-role matrix), `lint`, `test`, `packer-build`, `ci-image`.

**Escalation:** `minimal:` in `meta/test.yml` `machines:` adds a cell; `ubuntu:` list adds per-release cells (propagate down role-deps on push). **Local-debug:** `CI_BASE_REF=HEAD~5 mise run ci:detect-roles` previews matrix; `mise run ci:role-deps <helper>` lists consumers. CI secrets: [notes/ci_secrets_runbook.md](notes/ci_secrets_runbook.md).

## Commit & Pull Request Guidelines

Descriptive imperative subjects; prefix with role when it helps. Body: summary + motivation, max two paragraphs.

**Splitting:** use `/split_worktree_commits`. **Always `git diff --staged` before `git commit`.** **Never stage a vault-rendering template hunk-by-hunk** — files with rendered secrets must be staged whole-file or not at all.

## Security & Configuration Tips

Don't commit decrypted data; access secrets via `ansible-vault edit <path>`. WireGuard keys in `wireguard/` stay vaulted. Touching networking/DNS: run with `--limit`, apply Terraform only after review.

### Vault ids: `prod` vs `test`

Two passwords, two scopes ([ansible.cfg](ansible.cfg): `vault_identity_list = prod@vault-client.sh, test@vault-client.sh`, `vault_id_match = True`).

- `prod` — encrypts `group_vars/prod.yml` + prod host_vars. Local workstations only; never in CI.
- `test` — encrypts `group_vars/test.yml` + test host_vars. Available to CI as `HOMELAB_VAULT_PASSWORD_TEST` — never put a prod-blast-radius credential there.

[vault-client.sh](vault-client.sh): lookup per id: env `HOMELAB_VAULT_PASSWORD_<UPPER_ID>` (CI), then macOS keychain `homelab-vault-<id>`, then Linux `~/.config/homelab/vault-pass-<id>` (0400). Bootstrap: [notes/vault_setup.md](notes/vault_setup.md). New values: `encrypt_string --encrypt-vault-id prod` (or `test`).

## Someday

Open follow-ups live in [notes/SOMEDAY.md](notes/SOMEDAY.md) — backlog notes, not standing instructions; don't enact one without explicit operator confirmation.
