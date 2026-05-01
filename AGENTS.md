# Repository Guidelines

## Project Structure & Module Organization
`site.yml` orchestrates every host group and its roles. Role logic stays under `roles/<role>/` (tasks, templates, handlers), with shared defaults in `group_vars/` and host-specific overrides in `host_vars/`. `ansible.cfg` binds the repo to `hosts.ini` and `vault.sh`, so running from the root picks up the correct inventory and vault password automatically. Supporting folders: `packer/` builds the Podman/QEMU images, `terraform/` keeps Cloudflare DNS code and state, `wireguard/` stores VPN peers, and `test/` holds inventories, Dockerfiles, and logs.

### Pyinfra Migration Notes
`roles/<role>/<role>.py` hosts pyinfra ports of Ansible roles. Keep the function signature close to the original variable naming (`ansible_user`, `ansible_user_dir`, etc.) so deploy files can pass the same data that Ansible normally injects. Put any shared helpers inside `pyinfra_roles/` and ensure they handle both static files on disk and in-memory streams (see `file_put_with_validation`). Run ports via `uv run pyinfra inventory.py main.py` while iterating, and remember to keep sudo handling explicit by threading `_sudo` kwargs through helpers.

## Build, Test, and Development Commands
- Bootstrap tools: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`.
- Configure everything: `ansible-playbook -i hosts.ini site.yml --limit prod` (set `--tags` to narrow scope).
- Focus on one service or host: `ansible-playbook -i hosts.ini wireguard.yml -l lab --tags wireguard`.
- Manage DNS: `cd terraform && terraform init && terraform plan` before `terraform apply`.
- Refresh the integration image when the base OS changes: `cd packer && packer build qemu.pkr.hcl`.

## Coding Style & Naming Conventions
Use two-space YAML indentation and descriptive `name` values. Namespace variables per role (e.g., `samba_share_path`). Shell helpers in `packer/scripts/` should be Bash with `set -euo pipefail`. Before committing, run `ansible-playbook --syntax-check site.yml`, `terraform fmt`, and `packer fmt` to avoid churn.

### Role Conventions
- Centralize downloadable artifact metadata in `roles/<role>/vars/main.yml`. Keep the full URL **adjacent to its sha256** keyed by `ansible_architecture` (`x86_64` / `aarch64`), so a human auditing a checksum can see exactly which URL it pins without recomposing it from a version constant.
- For tasks that mutate state but should run only once (downloads, registrations, token fetches), gate them with `args: creates: <sentinel>` rather than `changed_when: false`. Skipped tasks don't count against the harness's idempotence check, and `changed_when: false` lies about what the task actually does.
- Test-only branches read the harness booleans `docker_test` and `qemu_test`, set in `host_vars/box-podman.yml` and `host_vars/box-qemu*.yml`. Use them in `when:` / `ternary` rather than per-machine name comparisons.
- Per-role test hooks live alongside the role's tasks:
  - `tasks/_setup.yml` — pre-role fixture bringup (e.g. start a local gitea instance for the role to register against).
  - `tasks/_verify.yml` — post-converge assertions about real system state (service active, file present, registration succeeded).
  - `tasks/_test.yml` — legacy alias for `_setup.yml`, still supported; many roles still use it.
- Roles that depend on staged check-mode tag tasks with `_check_stage1` … `_check_stage4` so the harness's check-mode pass can validate intermediate states. Apply the role tag plus the relevant stage tags to every task.

### Helper Roles
Several helper roles factor out duplicated patterns. Prefer them over re-implementing the boilerplate; a hand-rolled implementation in a service role is a migration candidate.

- `service_user` — `import_role: name: service_user, tasks_from: user` with `service_user_name: <svc>` creates the standard system group + nologin user (`/nonexistent` home) and `/mnt/services/<svc>` config dir, and exposes `<svc>_user.uid` / `.group` to the caller. Replaces the ~35-line group/user/dir prelude every service role used to repeat. Tag the import call with the role tag plus `_check_stage1`, `_check_stage2`. Roles with non-standard variants (alternate dir mode, extra groups like `media`/`render`, `createhome: true`) currently roll their own — extend the helper before duplicating.
- `podman_secret` — `import_role: name: podman_secret, tasks_from: secret` with `podman_secret_name` / `podman_secret_data` creates or rotates a podman secret. Tracks the data sha256 as a label on the secret itself and force-recreates only when content drifts (the upstream module is name-idempotent only and silently ignores rotations). Exposes `<name>_rotated` and `<name>_existed_before` so callers can wire restart triggers and detect post-bootstrap rotations of state that consumes the secret only at first creation (e.g. Django superuser via `SUPERUSER_PASSWORD` — fail loudly and tell the operator to run `manage.py changepassword` rather than rotate silently).
- `systemd_unit` — `import_role: name: systemd_unit, tasks_from: {template,copy,service}` installs/validates a unit, reloads systemd, and (for `service`) pre-pulls referenced podman images and starts/restarts the unit. Drive restarts via `systemd_unit_restart: "{{ systemd_unit.changed or <other>_rotated }}"` so config / secret changes propagate. Always prefer this over a hand-written `template:` + `systemd:` pair.

## Testing Guidelines
The harness lives in `test/` (Python, asyncio-based; the previous `*.sh` shims are gone).

- `test/testrole.py <role>` boots a Podman container by default and applies the role end-to-end. Pass `--machine {minimal,box,lab,pug}` to use a QEMU VM instead, `--ubuntu noble` to target a different release codename, `--keep` to leave the machine running for SSH debugging, and `--timeout SECONDS` to bound the run (default 30 min). The default flow runs staged check-mode, applies the role, runs it a second time to verify idempotence, then runs `_verify.yml` if present. Disable phases with `--no-checkmode` or `--no-idempotence` for tight dev loops. Output is colorized and written to `test/out/<machine>.<role>.ansi`; on failure the systemd journal is collected and the last 50 lines are tailed to stdout.
- `test/testall.py` fans out role × machine combinations across N concurrent workers (`--jobs N`, default 5). It accepts the same `--ubuntu`, `--checkmode`, `--idempotence` flags and forwards them. `--only-failed` rereads `test/out.log` and reruns just the failing rows. The joblog is tab-separated (`Role\tMachine\tRuntime\tExitval`) with the previous run preserved at `test/out.log.prev`.
- Exit codes are meaningful: `0` success, `1` converge failure, `124` per-test timeout, `125` idempotence failure, `130` user-cancelled. The joblog records the integer so you can sort/filter by failure mode.
- Keep the `homelab_net` Podman network and the `homelab:<release>` container image (and `packer-ubuntu-N` qcow2 overlays for the QEMU profiles) available locally, or rebuild them with the Packer command above. The QEMU profiles correspond to disk topologies: `minimal` (single cloud-init disk), `box` (1 disk), `pug` (3 disks), `lab` (9 disks).
- Include `ansible-playbook ... --check` and Terraform `plan` snippets in reviews so idempotence and drift are obvious.

## Commit & Pull Request Guidelines
History favors short, imperative subjects such as “Fix dnscrypt” or “Add profilarr”; prefix with a role when it helps clarity (`wireguard: rotate peers`). Each PR should summarize the motivation, list impacted hosts or roles, and link related issues. Mention which commands were run (`test/testrole.py`, `test/testall.py`, `terraform plan`, screenshots when relevant) and flag inventory, vault, or DNS updates so reviewers can re-run `vault.sh`.

## Security & Configuration Tips
Do not commit decrypted data; access secrets through `./vault.sh view/edit <path>` and keep them in `group_vars/*/vault.yml` or `host_vars/*/vault.yml`. WireGuard keys in `wireguard/` must remain vaulted and rotate with peer changes. When touching networking or DNS, run playbooks with `--limit` and apply Terraform only after review in a dedicated branch.
