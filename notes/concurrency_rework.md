# CI concurrency rework

## Problem

`test-nightly` (and `test` on multi-cell pushes) loses every matrix cell
except one to `Canceling since a higher priority waiting request for
lab-qemu-artifacts exists`.

Root cause: `test-role` declares a job-level `concurrency.group:
lab-qemu-artifacts` with a static string. GitHub evaluates job-level
concurrency per matrix combination, so every cell competes for one slot.
At most 1 running + 1 pending in a group; newly queued cells cancel the
previously-pending one. `cancel-in-progress: false` only protects the
running cell.

Secondary issue: even without the cancellation bug, the lab host only
has 1 `actions/runner` per repo today, so `max-parallel: 4` is
aspirational — cells would serialize at 1 anyway.

## Goal

- Max 4 VMs running concurrently across `packer-build` + `test-role`.
- Non-VM jobs (lint, ci-image, detect, notify, wait-for-\*) unrestricted.
- packer-build's qcow2 publish doesn't race against test-role's reads.

## Design

### A. Runner role refactor — podman, long-lived, scalable

Replace the per-repo host-process model with a templated podman-based
unit. Each container runs `actions/runner/run.sh` indefinitely.

- One templated unit `github_runner@.service` (per repo: e.g.
  `github_runner_homelab@.service`). Instance name = label suffix
  (e.g. `vm1`, `vm2`, `gen1`).
- Per-host instance map drives which instances run with which labels.
  Example for lab:

  ```yaml
  github_runner_instances:
    homelab:
      vm1: { labels: "self-hosted,lab-vm" }
      vm2: { labels: "self-hosted,lab-vm" }
      vm3: { labels: "self-hosted,lab-vm" }
      vm4: { labels: "self-hosted,lab-vm" }
      gen1: { labels: "self-hosted,lab" }
      gen2: { labels: "self-hosted,lab" }
    compta:
      gen1: { labels: "self-hosted,lab" }
  ```

- Each instance gets its own state volume + workdir bind-mount:
  - `/mnt/services/github_runner/<repo>/<instance>/` — runner state
    (`.runner`, `.credentials`, `_diag/`), persisted across container
    restarts so registration survives reboots / image bumps.
  - `/mnt/scratch/github_runner/workdir-<repo>-<instance>/` —
    actions/runner work dir (job checkouts).
- Runner image: thin custom image at
  `nexus.lab.fahm.fr/homelab/runner:<actions-runner-version>`. Built by
  a new `runner-image.yml` workflow (or extension of `ci-image.yml`),
  contains: actions/runner binary, `podman` CLI (for DooD ci-image
  builds), `bash`, `curl`, `git`, ca-certs.
- Container args (skeleton):

  ```
  podman run -d --name github_runner_<repo>_<instance> \
    --userns keep-id \
    --network host \
    --volume /run/user/<uid>/podman/podman.sock:/var/run/docker.sock \
    --volume /mnt/services/github_runner/<repo>/<instance>:/runner:rw \
    --volume /mnt/scratch/github_runner/workdir-<repo>-<instance>:/work:rw \
    --env RUNNER_NAME=<host>_<repo>_<instance> \
    --env RUNNER_LABELS=<labels> \
    --env REPO_URL=https://github.com/<owner>/<repo> \
    nexus.lab.fahm.fr/homelab/runner:<ver>
  ```

- Entrypoint script (in the image): if `/runner/.runner` missing, call
  `config.sh --token <reg-token>` (token passed via env at first start,
  unused thereafter). Then `exec ./run.sh`.

#### Registration token flow

Unchanged from today's host-process model in spirit:

- Role checks `/mnt/services/github_runner/<repo>/<instance>/.runner`.
  If missing, runs `gh api -X POST
  /repos/<owner>/<repo>/actions/runners/registration-token --jq .token`
  via `delegate_to: localhost` (operator's gh CLI auth — same source
  terraform uses).
- Fresh token passed into the container's environment for first start;
  container persists runner credentials in its state volume thereafter.
- **No long-lived PAT needed**. Scaling up = bump instance count, run
  ansible, role mints a fresh 1h token for each new instance.

#### What goes away

- Per-repo tarball extract + version sentinel (now in the runner image).
- `_register.yml`'s `unarchive` step + the 150 MB×N actions/runner
  trees on `/mnt/services/github_runner/<repo>/`.
- `--group-add 1` + `/var/run/docker.sock` symlink dance at the host
  level — replaced by bind-mounting github_runner's rootless podman
  socket directly into each runner container.

### B. Workflow `runs-on`

| Workflow / job                              | runs-on                       |
|---------------------------------------------|-------------------------------|
| `packer-build` / `build`                    | `[self-hosted, lab-vm]`       |
| `test` / `test-role`                        | `[self-hosted, lab-vm]`       |
| `test-nightly` / `test-role`                | `[self-hosted, lab-vm]`       |
| `lint` / `lint`                             | `[self-hosted, lab]`          |
| `ci-image` / `build`                        | `[self-hosted, lab]`          |
| `test` / `detect`                           | `[self-hosted, lab]`          |
| `test` / `notify-cross-cut`                 | `[self-hosted, lab]`          |
| `test` / `wait-for-packer-build`            | `[self-hosted, lab]`          |
| `test` / `wait-for-ci-image`                | `[self-hosted, lab]`          |
| `test-nightly` / `gate`                     | `[self-hosted, lab]`          |
| `test-nightly` / `notify`                   | `[self-hosted, lab]`          |
| `test` / `notify`                           | `[self-hosted, lab]`          |

Pool partitioning gives the 4-VM cap implicitly: 4 `lab-vm` runners ×
1 job each = 4 concurrent VMs. Non-VM jobs go to the disjoint `lab`
pool and never queue against VM jobs.

### C. Drop matrix-cancelling concurrency groups

Remove from:

- `test.yml` `test-role` (job-level `lab-qemu-artifacts`).
- `test-nightly.yml` `test-role` (job-level `lab-qemu-artifacts`).

Keep:

- `lint.yml` workflow-level concurrency (ref-cancel — unrelated).
- `ci-image.yml` workflow-level concurrency (ref-cancel — unrelated).
- `packer-build.yml` workflow-level `lab-qemu-artifacts` — serialises
  multiple packer-build dispatches. Doesn't cause cancellations
  (packer-build has no matrix), and prevents two concurrent builds from
  both writing to the qcow2 tree.

### D. flock for packer/test mutex

Pool partitioning prevents *more than 4* VMs but doesn't prevent
packer-build's publish from racing against a concurrent test-role's
qcow2 read. flock on `/mnt/scratch/qemu/.publish-lock`:

- **Packer (exclusive, brief)**: wrap the `install` post-processor's
  `rm -rf && mv` in [packer/qemu.pkr.hcl:361-370](packer/qemu.pkr.hcl#L361-L370)
  with `flock -x`. Sub-second hold.
- **Test (shared, brief)**: wrap `_create_overlay` +
  the qemu launch in [test/machine.py:1128-1138](test/machine.py#L1128-L1138)
  with `flock -s` on the same lockfile. Drop the lock once qemu has the
  backing file open (Unix unlink semantics protect from there).

Lockfile created once by the github_runner role (or first packer-build
that finds it missing). Both rw and ro mounts of `/mnt/scratch/qemu`
support flock.

The 60-min packer build does NOT hold the lock. Contention happens only
when a test starts during the ~1ms publish window — practically zero.

## Implementation order

Each step is independently testable; merge sequentially.

1. **Build the runner container image.**
   - New `Dockerfile.runner` (or `runner/Dockerfile`).
   - New workflow `runner-image.yml` mirroring `ci-image.yml`'s shape
     (push to nexus, pinned by sha + `:latest`).
   - Bootstrap: manually `workflow_dispatch` once before step 2.

2. **Refactor `roles/github_runner` to podman-based instances.**
   - New `github_runner_instances` var shape in `host_vars/lab.yml`
     (`vm1..4` + `gen1..2`) and `host_vars/compta.yml` if present.
   - Templated `github_runner@.service` (one per repo, instance =
     suffix).
   - `_register.yml` rewritten: per-instance state-dir + workdir
     creation, registration-token fetch gated on missing `.runner`,
     `podman_secret` for the token, container `--env`-driven first
     start.
   - Test on lab with the existing single-runner-per-repo model first
     (instance count = 1), then scale to 4+2 once green.
   - Decommission old per-repo host-process units via `_register.yml`'s
     idempotent path (stop + disable + remove unit file).

3. **Move workflows to the new label pools.**
   - Edit all `runs-on:` lines per the table in §B.
   - Drop the job-level `concurrency:` blocks per §C.
   - Verify nightly produces N parallel cells (not 1 → many cancelled).

4. **Add flock to packer + harness.**
   - Patch `packer/qemu.pkr.hcl` install post-processor.
   - Patch `test/machine.py` to flock-s around overlay creation +
     launch.
   - Create lockfile in role (touch-once) or on first packer run.

## Trade-offs & risks

- **DooD security**: containers bind-mount the host podman socket =
  effective host root for runner-image authors. Same trust boundary as
  today's host-process runner (it already had the socket symlinked).
  No regression.
- **ci-image workflow**: runs *outside* a `container:` block today,
  because it rebuilds the image other workflows use. With containerised
  runners, the steps run inside the runner container; the `podman
  build` step calls podman CLI which forwards to host podman via the
  bound socket. Need to verify nexus push + sha tagging still works
  through DooD (likely fine).
- **State persistence across image bumps**: runner state in
  `/mnt/services/github_runner/<repo>/<instance>/` persists across
  container restarts. An actions/runner version bump (image bump)
  *might* require re-running `config.sh` if the on-disk format changes
  between versions. Mitigation: image entrypoint reconciles —
  `config.sh remove --token <token>` + re-`config.sh` if the version
  marker in the volume mismatches the image's bundled binary.
- **Runner image source**: build our own. Small layer over a minimal
  base, pinned via mise.toml's `github_runner_version`. Mirrors the
  ci-image workflow shape.
- **Scale-down deregistration**: manual for v1. Removing an instance
  from `github_runner_instances` stops + removes the systemd unit and
  container; the operator runs `gh api -X DELETE
  /repos/<owner>/<repo>/actions/runners/<id>` and deletes the state
  dir by hand. Automate later if it becomes painful.

## Files touched (anticipated)

- `roles/github_runner/tasks/main.yml` — loop over instances dict, not
  repos list.
- `roles/github_runner/tasks/_register.yml` — per-instance dirs,
  podman-container service install.
- `roles/github_runner/templates/github_runner.service.j2` — rewritten
  for `podman run` + named container + per-instance env.
- `roles/github_runner/vars/main.yml` — drop the tarball URLs;
  `github_runner_image` (nexus URL + sha256).
- `host_vars/lab.yml`, `host_vars/compta.yml` — new
  `github_runner_instances` dict.
- New: `runner/Dockerfile`, `.github/workflows/runner-image.yml`.
- `.github/workflows/{packer-build,test,test-nightly,lint,ci-image}.yml`
  — `runs-on` + concurrency edits.
- `packer/qemu.pkr.hcl` — flock around install post-processor.
- `test/machine.py` — flock around `_create_overlay` + launch.
- `CLAUDE.md` — update the CI section: pool model, labels, flock.
