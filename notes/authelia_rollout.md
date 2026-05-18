# Authelia rollout plan

Outcome: replace ad-hoc per-service auth (or lack thereof) with a single
Authelia instance providing forward-auth + OIDC for the homelab.

## Architecture

- **Host:** `lab` (joins the existing central-services cluster: gitea,
  kuma, healthchecks, hyperdx, nexus).
- **Portal subdomain:** `auth.fahm.fr` (apex, **not** `auth.lab.fahm.fr`).
  - Rationale: session cookie scope is the longest common parent of
    every protected service. Services live under `*.lab.fahm.fr`,
    `*.pug.fahm.fr`, `*.bunk.fahm.fr`, and `*.fahm.fr`. The longest
    parent that contains all of them is `.fahm.fr`. Authelia portal
    must live on a hostname that shares that parent — `auth.fahm.fr`
    does, `auth.lab.fahm.fr` does not.
- **Cookie domain:** `.fahm.fr` (matches portal and every protected
  subdomain).
- **DNS:** add `auth.fahm.fr` A record in Cloudflare pointing to lab's
  public IP. Existing pattern: per-host A records are manually managed
  in Cloudflare (only `box.mhaf.fr` CI records are in terraform). One
  new manual record.
- **Cross-host auth_request:** pug/bunk nginx talks to authelia on lab
  over WireGuard (`10.123.0.2:9091`), not over public DNS. The portal
  redirect (browser-facing) goes to public `auth.fahm.fr`; server-side
  `auth_request` subrequests go direct over wireguard. Both paths
  reference the same Authelia daemon, just via different network paths.

## Backends

- **Storage:** SQLite at `/mnt/services/authelia/db.sqlite3`. No
  Postgres dependency. Authelia uses storage for: identity verification
  tokens, U2F/WebAuthn devices, TOTP secrets, OIDC consent records.
- **User backend:** file YAML at `/mnt/services/authelia/users.yml`,
  vaulted via ansible. Each entry has a bcrypt password hash, email,
  groups. Matches the existing "config-in-vault" pattern, no LDAP
  needed for <5 users.
- **Session:** in-memory (single Authelia instance, no Redis needed).
- **Notifier:** SMTP via existing Mailgun setup (matches `getmail` /
  cron-failure mail path). Used for password reset + identity
  verification emails.

## Secrets (PR 1 — vaulted, all via podman_secret helper)

- `authelia_jwt_secret` — signs identity-verification JWTs (e.g. reset
  emails).
- `authelia_session_secret` — encrypts session cookie payload.
- `authelia_storage_encryption_key` — encrypts at-rest sensitive
  fields in SQLite (TOTP secrets, WebAuthn keys).
- `authelia_smtp_password` — Mailgun SMTP creds.
- `authelia_users` — user database (dict, password hashes as
  argon2id strings).

PR 6 (first OIDC client migration) adds:

- `authelia_oidc_hmac_secret` — signs OIDC tokens (HS256).
- `authelia_oidc_issuer_private_key` — RSA private key for OIDC
  (RS256 JWT signing). 4096-bit, generated once, vaulted.

## 2FA

- TOTP (1Password supports this).
- WebAuthn / passkeys (modern browsers + 1Password).
- Enable both; configure default policy per-service in access control
  rules. Likely: `one_factor` for low-stakes UI (Homepage, Z2M, Wolweb,
  *arrs), `two_factor` for elevated (MinIO, Gitea, Pi-hole, MinIO).

## Rollout PRs

Each PR is independently revertable. Number = commit/PR boundary.

### PR 1 — `roles/authelia/` standup on lab (this PR)

- `roles/authelia/` (service_user, dirs, secrets, config, unit, nginx
  site, _verify, _setup).
- `host_vars/lab.yml` gets `authelia_users` (vaulted).
- `group_vars/prod.yml` gets shared secrets (vaulted): JWT, session,
  storage encryption key, OIDC HMAC, OIDC private key, SMTP password.
- `group_vars/all.yml` gets `service_ports.authelia: 9091`.
- `site.yml`: append `authelia` to the `hosts: box,lab` block.
- Cloudflare: `auth.fahm.fr` A record (manual, document in notes).
- Homepage bookmark.
- No consumers wired in. Authelia is reachable at `auth.fahm.fr`,
  serves the portal, lets users log in, displays the dashboard. That's
  the success condition for PR 1.

### PR 2 — `nginx_site_args.auth:` knob

- Add `auth: none|required|required_bypass_api` to the
  `nginx_site_args` contract.
- Extend `roles/nginx/templates/site.conf.j2` to render an
  `auth_request /internal/authelia` block + 401 redirect to portal
  when `auth: required` (or `required_bypass_api`, which adds a
  `location /api/ { ... no auth ... }` block).
- New `/etc/nginx/conf.d/authelia.conf` shipping the
  `/internal/authelia` proxy + upstream definition. On lab points at
  `127.0.0.1:9091`; on pug/bunk points at `10.123.0.2:9091`. Per-host
  via `host_vars`.
- `_verify` for one canary consumer (e.g. Homepage) flipped to
  `auth: required`.

### PR 3 — first protection wave (forward-auth only, no app changes)

Targets: `homepage`, `wolweb`, `z2m`, `profilarr`, `prometheus`,
`netdata`, `nut_server`, `keepalived_exporter`, `speedtest`. Each: one
line added to `nginx_site_args` dict. Zero risk of breaking app
internals.

### PR 4 — *arr wave (forward-auth + AuthenticationMethod=External)

Targets: `sonarr`, `radarr`, `lidarr`, `bazarr`, `tautulli`. Each
needs `config.xml` / equivalent flipped to `External` so the *arr
trusts the proxy. Bazarr also needs its empty `auth.api_key`
populated.

### PR 5 — bypass_api wave

Targets: `sabnzbd`, `transmission`, `influxdb`. Each needs custom
`location_conf` exempting its specific API path
(`/sabnzbd/api`, `/transmission/rpc`, `/api/v2/write`,
`/api/v2/query`).

### PR 6+ — OIDC migrations (one per service)

Order, by integration friction (lowest first):

1. **MinIO** — env-var-only.
2. **Paperless** — env-var-only.
3. **OpenProject** — built-in UI for OIDC providers.
4. **Gitea** — CLI bootstrap, exists today as a manual task.
5. **Kuma** — UI-driven, last (UI state is per CLAUDE.md
   intentionally not in IaC).

### PR 7 — Pi-hole defense-in-depth

Forward-auth in front of Pi-hole admin. Keep Pi-hole's own
password as the second wall. Double-login is acceptable for the
rarely-visited admin page.

### Deferred indefinitely

- **HomeAssistant** — wait for upstream OIDC (or accept the
  community custom_component path later).
- **Nexus OSS rutauth** — works but awkward; revisit if Sonatype
  ever opens OIDC in OSS.
- **HyperDX** — OSS has no SSO, ingestion contract is separate.
- **Healthchecks** — public ping endpoint must stay open.
- **Jellyfin SSO plugin** — manual plugin install; defer until the
  rest of the stack is stable.

### Will not migrate

- **Plex** — uses plex.tv accounts only, no path.
- **Overseerr** — Plex SSO is already fine, no value add.
- **Eaton IPP** — proprietary auth, rarely visited.
- **csplogger**, **journald_exporter**, **HyperDX `/v1/logs`**,
  **Healthchecks ping endpoint** — public-by-design machine
  endpoints.

## Test plan for PR 1

- `mise run lint`
- `test/testrole.py authelia` against `box` — boots VM, applies role,
  hits `/api/health`, verifies portal returns the login page,
  asserts `/api/authz/auth-request` returns 401 unauthenticated,
  asserts `/.well-known/openid-configuration` is well-formed.
- After merge: see "Manual bootstrap on lab" below.

## Manual bootstrap on lab (run once after merge)

PR 1 ships the role + a hardcoded test user for the box fixture.
Real prod users live in vault and the operator owns the bootstrap.

### 1. Generate Authelia secrets (one-time)

Run on a workstation with `authelia` available via podman:

```sh
# Random alphanumeric secrets (64+ chars each, per Authelia
# recommendation). Pipe each into ansible-vault encrypt_string.
for k in authelia_jwt_secret authelia_session_secret \
         authelia_storage_encryption_key; do
  echo "$k:"
  podman run --rm authelia/authelia:4.39.19 \
    authelia crypto rand --length 64 --charset alphanumeric
done

# Mailgun SMTP password: pull from 1Password (op://homelab/mailgun-smtp/password).
# OIDC keys come later in PR 6 (first OIDC migration).
```

### 2. Generate per-user argon2id hashes (one-time, per user)

```sh
podman run --rm -it authelia/authelia:4.39.19 \
  authelia crypto hash generate argon2 -- --password 'YOUR_PASSWORD'
# Copy the resulting $argon2id$... string into authelia_users.<name>.password_hash
```

### 3. Vault entries to add to `group_vars/prod.yml`

Test-fixture equivalents are already in [group_vars/test.yml](group_vars/test.yml)
(plain stubs, no vault), so this section is prod-only.

All as `!vault |` blocks. Encrypt each with `--encrypt-vault-id prod`:

```yaml
authelia_jwt_secret: !vault |
  ...
authelia_session_secret: !vault |
  ...
authelia_storage_encryption_key: !vault |
  ...
authelia_smtp_password: !vault |
  ...

authelia_users:
  ak:
    displayname: Adrien
    password_hash: !vault |
      ...
    email: adrien.kohlbecker@gmail.com
    groups:
      - admins
  # Add additional users for family / Plex consumers as needed.
```

The per-user `email` and `displayname` can stay cleartext (no
security value in vaulting them); the password hash is the only
sensitive field.

### 4. Cloudflare DNS

Add an A record in Cloudflare (not yet terraform-managed for
fahm.fr per-record state):

- `auth.fahm.fr` → lab's public IP (same as the other `*.fahm.fr`
  apex services use)
- Proxy status: same as existing apex records (orange-cloud if
  others use it).

### 5. Apply

```sh
mise run ansible -- --limit lab --tags authelia
```

Then visit `https://auth.fahm.fr/`, log in with one of the vaulted
users, register TOTP via 1Password, register WebAuthn / passkey via
your browser/OS keychain.
