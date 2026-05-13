# Migration: Gitea Actions → GitHub Actions

## Context

The repo currently runs CI on Gitea Actions via a self-hosted `act_runner` on
lab. See [notes/gitea-actions-ci.md](gitea-actions-ci.md) for the existing
design — almost all of it (lab-runtime image, detect-roles, role-deps,
mail-cross-cut, mail-failures, per-role fan-out, activity-gated nightly,
packer-build mutex, `--workdir-parent` harness changes) stays intact. This
plan describes only the **delta** — what swaps out, what stays, and what gets
*simpler* on GitHub.

The two motivations:

1. **Stability.** Gitea Actions still has rough edges (we burned a week on
   forgejo/runner#525 / go-gitea/gitea#25179 — dynamic matrix unbinds the
   job-DAG and the runner reports skipped jobs as failed with status=4 — see
   the comment block in [.gitea/workflows/test.yml:44-50](../.gitea/workflows/test.yml#L44-L50)).
   GitHub Actions has had a decade of production hardening on the same
   surface area.
2. **Real dynamic matrix.** The static-superset-plus-`if:`-filter workaround
   in test.yml / test-nightly.yml exists *only* because Gitea materialises
   the job DAG before `needs.<job>.outputs` is resolved. GitHub evaluates
   `strategy.matrix.include: ${{ fromJson(needs.detect.outputs.matrix) }}`
   correctly — that's the canonical pattern. We can drop ~110 hand-maintained
   matrix rows from each test workflow, and delete `ci:gen-test-matrix` and
   `ci:lint-test-matrix` entirely.

## Billing: will GitHub charge for minutes?

**No** — provided we keep using a self-hosted runner on lab.

GitHub Actions billing only applies to **GitHub-hosted runners** (their VMs,
billed per-minute: Linux 2-core = $0.008/min on private repos beyond the free
tier). **Self-hosted runners are free for all repositories** — public,
private, all plans (Free / Pro / Team / Enterprise). Quote from the GitHub
docs: *"Jobs that run on self-hosted runners are free and don't count against
the minutes included with your account."*

Other line items to be aware of, none of which we'd hit in practice:

- **Artifact storage**: Free plan includes 500 MB free for private repos
  (unlimited for public). Beyond that: $0.008/GB-day. Our on-failure
  artifacts (journal/dmesg/boot.log/workdir) average ~50-200 MB per failed
  cell; 14-day retention. A typical week (few failed cells) stays well under
  500 MB. If a nightly mass-fails, we could brush the cap — mitigation:
  shorten `retention-days` or compress before upload.
- **Actions cache**: 10 GB per repo, free. We don't use it.
- **GitHub Packages**: not used.
- **LFS / Codespaces / Copilot**: not in scope.

KVM acceleration *requires* a self-hosted runner regardless of billing —
GitHub-hosted runners are themselves VMs and don't expose `/dev/kvm`. So the
choice is forced: self-hosted = free.

Tangentially: GitHub *will* disable scheduled workflows in private repos
after 60 days of repo inactivity (no commits). That affects
`test-nightly.yml`. Our 25h activity gate already skips idle days, so this
is only a problem if the *repo* goes 60 days without any push — unlikely on
this homelab cadence, but worth knowing.

## Repo location

GitHub Actions only fires on pushes to a GitHub remote. Two routings:

- **A. GitHub becomes origin.** Re-point `git remote set-url origin
  git@github.com:adrienkohlbecker/homelab.git`. Gitea optionally stays as a
  read-only mirror (`gitea` remote, or Gitea's pull-mirror feature pointed
  at GitHub).
- **B. Dual-remote.** Keep Gitea as origin, add `github` as a second remote,
  push to both. Reasonable transition state; final state should be A so
  there's one source of truth.

**Visibility**: this plan assumes a **private** repo on GitHub. Vaulted
secrets in `group_vars/*.yml` are safe-by-design to publish (that's the
point of vault), but hostnames, IP topology, service paths, and the fact
that prod runs on `lab.fahm.fr` are reasonable to keep private. Confirm
before pushing.

Repo size sanity-check: `du -sh .git` should be well under GitHub's 100 GB
soft cap; the repo's mostly text + a few small icon binaries.

## What stays vs what swaps

**Stays (no changes):**
- `lab-runtime` container image (Dockerfile, build wiring in act_runner role)
- `mise-tasks/ci/{detect-roles, role-deps, mail-cross-cut, mail-failures}`
- The 25h activity gate in nightly
- `lab-qemu-artifacts` concurrency group semantics (writer = packer-build,
  readers = test / test-nightly)
- Per-cell `--workdir-parent /lab-runtime-workdir` and per-run subdir
  bind-mount pattern
- `_packer` synthetic role + sentinel hand-off from packer-build to test
- Test harness (`test/testrole.py --workdir-parent`, on-failure artifact
  collection)
- Variant escalation via `ci-lab-roles.txt`
- Secrets surface: `MAILGUN_API_KEY`, `MAILGUN_DOMAIN`,
  `HOMELAB_VAULT_PASSWORD_TEST`

**Swaps:**
- `act_runner` (gitea's go binary) → `actions/runner` (github's go binary,
  same shape; runs as a systemd unit)
- `.gitea/workflows/*.yml` → `.github/workflows/*.yml`
- `.gitea/ci-lab-roles.txt` → `.github/ci-lab-roles.txt` (path-update in
  `detect-roles` script)
- `act_runner` role → `github_runner` role (rename for clarity; same
  structure)
- Workflow files: dynamic matrix replaces static superset; job-level `if:`
  replaces step-level `if:` for skip handling; `actions/upload-artifact@v3`
  → `@v4`; drop the `wait-for-packer-build` polling job in favour of native
  cross-workflow concurrency *or* `workflow_run` (see below)
- Container `--add-host=gitea.lab.fahm.fr:host-gateway` option goes away
  (no longer needed; lab-runtime doesn't talk to local Gitea)
- Container `--group-add 1` stays — same rootless-podman gidmap reasoning,
  unrelated to the runner platform
- Registration model: act_runner uses a Gitea-issued registration token
  scoped to the repo; GitHub uses a runner registration token from
  `POST /repos/{owner}/{repo}/actions/runners/registration-token` (or a
  PAT-derived `gh api` call) — fetched fresh on registration, expires in 1h

## Runner replacement: actions/runner on lab

`actions/runner` is GitHub's first-party Go binary, distributed as tarballs
from `github.com/actions/runner/releases`. Same install pattern as
act_runner: download to `/usr/local/bin/`, register, run as a systemd unit
under the dedicated user.

Concrete config:

- **Binary**: `actions-runner-linux-x64-<ver>.tar.gz`, sha256-pinned in
  `roles/github_runner/vars/main.yml` per the project's url+sha convention.
  Picked up by `get_url` with `checksum:` (mirrors `act_runner` install task).
- **User**: `github_runner` (analogous to `act_runner`). Same
  `service_user_extra_groups: [kvm]` so `/dev/kvm` is delegatable. Same
  `/mnt/scratch/github_runner/` layout (cache, workdir, lab-runtime-workdir).
  Same `loginctl enable-linger`.
- **Registration**:
  - Prod: registration token fetched via `gh api -X POST
    /repos/$OWNER/$REPO/actions/runners/registration-token --jq .token` at
    apply time, then `./config.sh --url https://github.com/$OWNER/$REPO
    --token <token> --name lab --labels self-hosted,lab-runtime
    --unattended --replace`. Idempotent via `creates:
    /mnt/services/github_runner/.runner` sentinel.
  - The `gh` CLI on the controller needs a PAT with `admin:repo_hook`
    (legacy) or fine-grained `Actions: read+write` permission on the
    target repo. Resolved through 1Password — declare
    `GITHUB_TOKEN = "op://..."` in `mise.toml` `[env]`. Aligns with the
    existing `feedback_op_secrets_via_env.md` pattern (no shelling out to
    `op read` from ansible).
  - Test (`qemu_test`): no GitHub registration in CI's own test of the
    runner role. The role's `_setup.yml` currently boots a local Gitea
    instance to issue tokens — port that to spin up a GitHub Enterprise
    Server / mock, **or** declare the registration step out-of-scope
    under `qemu_test` and verify only the static config (binary present,
    user/group set up, systemd unit valid). The mocking is awkward;
    skipping the registration step under qemu_test is the pragmatic call.
- **Labels**: `self-hosted,lab-runtime`. Drop the host-name and ubuntu-codename
  labels — they're unused.
- **Container support**: GitHub's runner spawns container jobs via the
  Docker socket (env: `DOCKER_HOST`). We point it at the rootless podman
  docker-compat socket (`unix:///run/user/<uid>/podman/podman.sock`), same
  as act_runner does today.
- **No `valid_volumes` whitelist**: GitHub's runner doesn't have this
  Gitea-specific safety knob. Workflows declare volumes in `container.options`
  and they're applied as-is. The implicit security tradeoff is real but
  acceptable: workflows are repo-controlled, and we don't run untrusted
  PRs (we don't take PRs at all). For belt-and-braces, the runner unit's
  systemd hardening (NoNewPrivileges, ProtectSystem=strict, capped uid via
  the github_runner user) limits the host blast radius.
- **systemd unit**: `roles/github_runner/templates/github_runner.service.j2`
  — `ExecStart=/usr/local/bin/run.sh` (the runner ships a `run.sh` wrapper
  that sources `.env` and invokes the binary). `Environment=DOCKER_HOST=...`
  to point at the rootless socket. `Restart=always`. Same enable+start
  semantics as act_runner.

Verification ladder for runner bringup:

1. `id github_runner` lists `kvm`.
2. `sudo -u github_runner podman run --rm --device /dev/kvm --group-add 1
   ubuntu:24.04 ls -la /dev/kvm` shows the device (not "permission denied").
3. `sudo -u github_runner podman images | grep lab-runtime` present after
   role apply.
4. Runner shows online in GitHub repo settings → Actions → Runners.
5. Trivial workflow_dispatch (a one-step `echo hello` workflow) completes
   successfully.

## Workflow files

Move from `.gitea/workflows/` to `.github/workflows/`. File contents
diverge as follows.

### Common across all four workflows

- `runs-on: [self-hosted, lab-runtime]` (array form — GH requires
  `self-hosted` to be present alongside any custom label on self-hosted
  runners; Gitea was happy with a bare custom label).
- Each workflow declares `container.image: lab-runtime:latest` and the
  same `--device /dev/kvm` + `--group-add 1` + bind-mounts in
  `container.options`. No `--add-host` needed.
- `defaults.run.shell: bash` stays — GitHub's runner also defaults to bash
  on Linux self-hosted, but explicit is clearer.

### lint.yml

Functionally identical. Same single job, same `cancel-in-progress: true`
on `lint-${{ github.ref }}`. Only change: `runs-on: [self-hosted,
lab-runtime]`.

### test.yml

Three structural changes vs the Gitea version:

1. **Dynamic matrix.** Replace the BEGIN/END superset block + per-cell
   `if: contains(...)` filter with:

   ```yaml
   test-role:
     needs: [detect, wait-for-packer-build]
     if: needs.detect.outputs.matrix != '[]'
     strategy:
       fail-fast: false
       max-parallel: 2
       matrix:
         spec: ${{ fromJson(needs.detect.outputs.matrix) }}
   ```

   `detect-roles` already emits a flat JSON array of `"role:variant"`
   strings, which is exactly the right shape for `strategy.matrix.spec`.
   No regen task, no lint task, no static superset.

2. **Job-level `if:` for notify-cross-cut and wait-for-packer-build.**
   GitHub correctly handles skipped jobs as `result: skipped` (not
   `failure`), so downstream `needs:` chains don't jam. Move the `if:` from
   step-level back up to job-level. Removes the no-op runner start that
   Gitea forced us into.

3. **Drop `wait-for-packer-build` polling** — *if* native cross-workflow
   concurrency suffices. See "Concurrency strategy" below; I'd keep the
   sentinel-based wait for the same-push case (it's robust and we already
   have it working), and rely on `concurrency` for the cross-push case.

4. **`actions/upload-artifact@v4`.** v3 is deprecated (sunset 30 Nov 2024
   on github.com; still works on GHES). v4 requires unique artifact names
   per workflow run — already true in our case (`testout-${ROLE}-${VARIANT}`
   is unique per matrix cell). Also v4 doesn't auto-zip; the path glob
   semantics are the same. Drop-in.

### test-nightly.yml

Same three changes as test.yml. The `gate` job's activity-window check is
unchanged. Matrix expansion uses `fromJson(needs.gate.outputs.matrix)`
directly.

### packer-build.yml

Smallest change: `runs-on: [self-hosted, lab-runtime]` and that's it. The
concurrency group stays the same (`lab-qemu-artifacts`, `cancel-in-progress:
false`), and the sentinel-write step stays so test.yml's wait-job can
still poll for it.

## Concurrency strategy: same-push ordering

The hard case: a single push touches `packer/qemu.pkr.hcl`, which triggers
*both* `packer-build.yml` and `test.yml` (the latter via `_packer` in the
matrix). test.yml needs to wait for packer-build to finish so the `_packer`
test cell reads the rebuilt qcow2, not the stale one.

GitHub's `concurrency` group serialises but doesn't *order* — if both
workflows enter the group simultaneously, GitHub picks one to run and
queues the other. That's fine for the "two unrelated pushes" case, but
doesn't help with "this push needs packer-build to complete first."

Three options:

- **Keep the sentinel.** test.yml's `wait-for-packer-build` job polls
  `/mnt/scratch/qemu/_sentinels/complete-$SHA`. Robust, already working,
  no additional dependency. Cost: extra container start per push that
  touches packer/. Recommended.
- **`workflow_run` trigger.** Make test.yml *additionally* triggered on
  `workflow_run: [packer-build], types: [completed]`, and gate on
  `if: github.event.workflow_run.conclusion == 'success'`. Cleaner DAG
  but: (a) two test runs for the same SHA when packer/ changes (one from
  push, one from workflow_run), (b) `workflow_run` always runs on the
  default branch's workflow file which complicates feature-branch testing.
- **Manual sequencing.** Have packer-build dispatch test.yml when it's
  done. Trades native push-driven CI for explicit chaining; not worth
  the complexity reduction.

Go with the sentinel. The job is 1 cheap container start and the code
is already written and tested.

## detect-roles + role-deps

Three small edits in `mise-tasks/ci/detect-roles`:

- `LAB_ROLES_FILE=".gitea/ci-lab-roles.txt"` → `.github/ci-lab-roles.txt`.
- Comment in `build_matrix()` about Gitea 1.21.11 not expanding
  `${{ matrix.<obj>.<field> }}` is now stale — *keep* the flat-string
  format anyway (the workflow's `${SPEC%%:*}` / `${SPEC##*:}` parsing is
  simpler than fanout objects), but update the comment to "kept flat for
  shell parse simplicity, not platform constraint."
- `CROSS_CUT_RE` is unchanged.

`role-deps` is platform-agnostic, no changes.

## mail-cross-cut + mail-failures

Both already use Mailgun's HTTP API directly, with secrets injected as env
vars. Drop-in. One detail: `mail-cross-cut` builds a "dispatch via the
Actions tab" URL that currently points at Gitea — re-templating it for
`$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/workflows/test.yml` is a
one-line change.

## Harness changes

None. The harness already supports `--workdir-parent` (Phase 2 of the
gitea plan was done) and on-failure artifact collection. The artifact-glob
patterns in workflows are unchanged.

## Files affected

**Renamed:**
- `roles/act_runner/` → `roles/github_runner/` (full directory move)
- All `act_runner` references in `site.yml`, `host_vars/lab.yml`, and any
  cross-references

**Moved:**
- `.gitea/workflows/lint.yml` → `.github/workflows/lint.yml`
- `.gitea/workflows/test.yml` → `.github/workflows/test.yml`
- `.gitea/workflows/test-nightly.yml` → `.github/workflows/test-nightly.yml`
- `.gitea/workflows/packer-build.yml` → `.github/workflows/packer-build.yml`
- `.gitea/ci-lab-roles.txt` → `.github/ci-lab-roles.txt`

**Modified:**
- `mise-tasks/ci/detect-roles` — file-path constants, stale comment
- `mise-tasks/ci/mail-cross-cut` — run-URL template
- `mise.toml` — add `GITHUB_TOKEN = "op://..."` in `[env]` for runner
  registration
- `roles/github_runner/tasks/main.yml` — registration command, label list,
  systemd unit env, drop `valid_volumes` (no equivalent)
- `roles/github_runner/templates/github_runner.service.j2` — new unit file
  (mostly a copy of `act_runner.service.j2` with binary path + run.sh wrapper)
- `host_vars/lab.yml` — `subid_extra_subgid_groups` user rename
- `CLAUDE.md` — replace `## Continuous Integration` section: github runner
  install path, dynamic matrix (drop the static-superset paragraph), update
  the docker-compat-socket `--group-add 1` rationale (unchanged in
  substance), repoint workflow paths

**Deleted:**
- `mise-tasks/ci/gen-test-matrix` (no longer needed — matrix is dynamic)
- `mise-tasks/ci/lint-test-matrix` (no drift to check)
- The `ci:gen-test-matrix` / `ci:lint-test-matrix` mise task entries
- The BEGIN/END AUTO-GENERATED MATRIX blocks in both test workflows

## Implementation phases

Each phase ends in a working state, with the prior CI still functional
until the cutover. Commit between phases.

### Phase 0 — repo on GitHub (no CI yet)

1. Create the GitHub repo (private, default branch `master`). Don't enable
   Actions in repo settings yet (default-on but no workflows in `.github/`
   means nothing fires).
2. `git remote add github git@github.com:adrienkohlbecker/homelab.git`
   and `git push github master`. Keep Gitea as origin for now.
3. Confirm repo size, branch structure, tags all came across cleanly.

### Phase 1 — github_runner role

1. `git mv roles/act_runner roles/github_runner` and update all
   cross-references (`site.yml`, host_vars, role internals).
2. Replace `act_runner` binary install with `actions/runner`:
   - `roles/github_runner/vars/main.yml`: new url+sha per-arch for
     `actions-runner-linux-{x64,arm64}-<ver>.tar.gz`
   - `tasks/main.yml`: `get_url` to a temp tarball, `unarchive` into
     `/mnt/services/github_runner/`, `creates:`-gated registration via
     `gh api ... | xargs ./config.sh`
3. Update `templates/github_runner.service.j2`: `ExecStart=./run.sh`
   (relative to `/mnt/services/github_runner/`), `WorkingDirectory=`,
   `Environment=DOCKER_HOST=...`.
4. Drop the `config.yml.j2` (act_runner-specific) — actions/runner has no
   equivalent yaml config. The label set is passed at register time and
   persisted to `.runner` (json); container options come from workflow
   files; cache server isn't a runner concern (GHA cache uses an external
   service). Move the `container.options` defaults (the `--group-add 1`
   alias) into the workflow files' `container.options:` instead, since
   there's no runner-side knob for it.
5. Add `GITHUB_TOKEN = "op://..."` to `mise.toml` `[env]` (1Password
   resolves at mise load).
6. Re-apply the role to lab. Verify the runner appears online in GitHub →
   repo → Settings → Actions → Runners.
7. Run a hand-written one-off `.github/workflows/smoke.yml` (single job,
   `runs-on: [self-hosted, lab-runtime]`, single `echo` step) to verify
   the full path: lab-runtime container starts, the runner can pull the
   image, steps execute, the run reports success.
8. **Commit**: `github_runner: replace act_runner with actions/runner`.

### Phase 2 — workflows

1. Create `.github/workflows/lint.yml` — direct port.
2. Create `.github/ci-lab-roles.txt` — direct copy.
3. Patch `mise-tasks/ci/detect-roles` for the new path. Patch
   `mail-cross-cut` for the GitHub run URL template.
4. Create `.github/workflows/test.yml` — dynamic matrix form, job-level
   `if:` for notify-cross-cut and wait-for-packer-build, `upload-artifact@v4`.
5. Create `.github/workflows/test-nightly.yml` — same dynamic matrix
   form. Same artifact bump.
6. Create `.github/workflows/packer-build.yml` — direct port,
   `runs-on: [self-hosted, lab-runtime]`.
7. Push a smoke commit. Verify all four workflows resolve and produce
   expected fan-out.
8. **Commit**: `ci: port four workflows to GitHub Actions with dynamic matrix`.

### Phase 3 — secrets

Populate via `gh secret set`:

```
gh secret set MAILGUN_API_KEY -b "$(./vault.sh view group_vars/prod.yml | yq '.mailgun_api_key')"
gh secret set MAILGUN_DOMAIN -b "..."
gh secret set HOMELAB_VAULT_PASSWORD_TEST -b "..."
```

Verify in `gh secret list`. No commit (Settings change only).

### Phase 4 — cutover

1. Disable Gitea Actions in [roles/gitea/templates/app.ini.j2](../roles/gitea/templates/app.ini.j2)
   (`[actions] ENABLED = false`).
2. Remove the `.gitea/workflows/` tree. Keep the directory itself only if
   we want app.ini metadata; otherwise delete.
3. `git remote set-url origin git@github.com:adrienkohlbecker/homelab.git`,
   `git remote add gitea git@gitea.lab.fahm.fr:adrienkohlbecker/homelab.git`
   if we still want push-mirror to lab.
4. (Optional) Configure Gitea pull-mirror from GitHub for read-only
   browsing on the lab UI.
5. Push a final commit. Confirm:
   - lint workflow green
   - touching `roles/healthchecks/...` → only `healthchecks:box` cell runs
   - cross-cut path (touching `group_vars/all.yml`) → mail arrives
   - workflow_dispatch with `roles=ALL` → full universe runs
   - nightly schedule fires the next morning (gate passes if there were
     commits)
6. **Commit**: `ci: complete migration to GitHub Actions; remove .gitea/`.

### Phase 5 — cleanup

1. Delete `mise-tasks/ci/gen-test-matrix` and `lint-test-matrix`. Remove
   their entries from `mise.toml`.
2. Update CLAUDE.md `## Continuous Integration` section: github paths,
   dynamic matrix, no superset, runner install, registration flow.
3. **Commit**: `ci: drop static-matrix scaffolding; document GHA flow`.

## Rollback

If the cutover surfaces unexpected behaviour:

- **Runner refuses to register / can't reach github.com**: lab → github.com
  is outbound HTTPS, should work. If a firewall is in the way, lab
  already has `iptables` egress allowing 443. Verify with `curl -sSf
  https://api.github.com/zen`.
- **`/dev/kvm` works in lab-runtime but rootless podman docker-socket
  bridge breaks**: actions/runner's docker-compat usage is identical to
  act_runner's (the runner shells out to `docker` CLI which honours
  `DOCKER_HOST`). If something breaks here, fall back to host-runner
  mode (set `runs-on: [self-hosted, lab-host]`, drop `container:`, install
  steps run on the host directly) — Gitea path didn't have to use this
  because of `valid_volumes` constraints, but on GHA there's no such
  restriction.
- **Cross-workflow concurrency doesn't queue as expected**: sentinel
  approach is the same as Gitea; it just works. The only thing being
  removed is the static-matrix workaround, not the mutex.
- **Hard rollback**: re-enable Gitea Actions in `app.ini`, restore
  `.gitea/workflows/`, re-point origin to Gitea. The `act_runner` role
  is in git history. ~10 min of work.

## Open questions

(System reminder asks not to pause for clarifications, so I'm noting
these as decisions to revisit *during* implementation rather than
blockers.)

- **Repo visibility**: defaulting to private. If the user prefers
  public (homelab repos commonly are), nothing technical changes — just
  the visibility setting at repo creation.
- **Repo owner**: defaulting to `adrienkohlbecker` (the GitHub account).
  An org would change the owner segment of the registration URL but
  nothing else.
- **Keep Gitea Actions as a fallback for a transition period?** Could
  leave both runners co-existing for a couple of weeks. Cost: two runners
  on the same 4-capacity host. The `lab-qemu-artifacts` concurrency
  group is per-platform, so both could try to write the qcow2 tree —
  bad. Recommend a clean cutover after Phase 3's verification, not
  a parallel run.
- **`act_runner._setup.yml` portage**: the role's own `_verify.yml`
  currently registers against a fixture Gitea. For the github_runner
  role, the equivalent fixture (a local GitHub Enterprise Server)
  doesn't exist as freeware. Realistic options: (a) under `qemu_test`,
  verify only the static layout (binary present, user/group, systemd
  unit syntax) and skip registration; (b) mock the
  `actions/runners/registration-token` endpoint via a stub HTTP server.
  Recommend (a) — registration in test would test gh.com's API surface,
  not our role. The `_verify.yml` memory says "exercise the live path";
  this is the explicit fallback for "live path requires an external
  service we can't reasonably stand up in test."

## Wins after migration

- ~110 hand-maintained matrix rows → 0 (per workflow, ×2 workflows)
- 2 mise tasks deleted (`gen-test-matrix`, `lint-test-matrix`)
- Step-level `if:` workarounds for skip-handling → job-level (cleaner)
- One CI platform to track instead of two (Gitea Actions versions +
  act_runner versions)
- Workflow run URLs are stable and publicly addressable (useful in
  commit messages, issues, status badges)
- `gh run watch`, `gh run view`, `gh workflow run` CLI for local
  observability — much better than Gitea's web-only UI

## Costs after migration

- Self-hosted runner setup is one-time but real (~half a day end-to-end
  including registration, smoke test, secret population)
- Lose Gitea Actions' `valid_volumes` whitelist (mitigated: repo is
  trusted)
- GitHub's 60-day inactivity-disables-cron rule could bite if the repo
  goes long-quiet (mitigated: nightly's 25h activity gate is the right
  filter, but the *outer* GitHub disable is independent — set a
  calendar reminder to push a trivial commit if 50 days pass)
