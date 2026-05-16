# Test consolidation: collapse to box + minimal

## Motivation

The `box` / `lab` / `pug` test variants exist today because they map to three
prod-host shapes. Only `lab` and `pug` are real prod hosts (see
`hosts.ini` — `box` is only in `[test]`), so the "box variant" is
a synthetic test fixture with no prod counterpart. Per-prod-host fan-out
in push CI doubles or triples per-role wallclock without proportional
coverage: ZFS / EFI / systemd / Ubuntu are identical across variants;
disk count is invisible to ansible roles. The only meaningful axis
between variants is the `host_vars/*.yml` values, and once you accept
that lab.yml / pug.yml combinations won't be validated against role
changes pre-apply (acceptable — nightly + manual remains the safety
net), the multi-variant CI fan-out costs more than it returns.

## End state

- **Push CI fan-out**: `box` + `minimal` only. `lab` / `pug` per-push
  cells go away. Nightly retains the full universe via existing matrix.
- **Single fat-box fixture**: mirror rpool + apoc + dozer + tank + mouse,
  matching lab-prod's separate-pool dataset layout (the more interesting
  consumer code path). `host_vars/box.yml` absorbs `host_vars/box-qemu.yml`
  directly — box is already test-only so no prod divergence concern.
- **Minimal becomes its own inventory host**: `minimal` in `[test]`,
  `host_vars/minimal.yml` is the renamed `box-qemu-minimal.yml`.
- **Packer builds three variants**: `box` (fat union, the CI image),
  `pug` (single rpool + apoc, matches pug-prod), `lab` (mirror rpool +
  dozer/tank/mouse, matches lab-prod). Pug + lab images are not
  consumed by CI tests — their job is to exercise the packer/chroot/
  provision scripts on the two real prod shapes.
- **Pool creation moves into packer**: `test/disks/{lab,pug}.sh`'s
  zpool-create logic is absorbed into `packer/scripts/provision.sh`
  (or a new `pools.sh` sourced from it), dispatched on `SOURCE_NAME`.
  Pools are exported before VM shutdown so they import on next boot.
- **Harness shrinks**: `QemuMachineSpec.{extra_disks,disk_setup_script}`
  retire. `_qemu_ansible_args` drops the `-qemu` suffix-picking and the
  `qemu_test` JSON injection (those vars move into the host_vars files
  themselves, loaded natively by inventory).
- **test/disks/ retires**.
- **CI lab-roles list retires**; minimal-roles list stays (still
  meaningful axis: cleanup's snap-removal path needs preinstalled snapd).

## What we gain

- One default fan-out variant per push instead of "default plus
  per-role-escalation"; matrix cardinality drops.
- Harness loses ~40 lines of conjure-blank-qcow2 logic.
- `host_vars/*-qemu*.yml` overlay machinery retires; ansible loads
  test host_vars from inventory normally.
- Pool layout regressions surface at packer build time on a real
  prod-shaped image (pug or lab variant), not only when someone tests
  a role on `--machine lab` locally.

## What we lose, and why we accept it

- **lab.yml / pug.yml host_vars don't get validated by push CI**:
  combinations like `zfs_arc_max: 8GiB`, `zfs_trim_pools: [rpool]`,
  pug's autobackup-source-pointing-at-lab, etc. We accept this — push
  CI's job is "does the role logic work"; lab/pug-specific values are
  caught by nightly + by deliberate manual `testrole.py --machine lab`
  before prod apply.
- **Pool-layout iteration cost goes up**: editing `test/disks/lab.sh`
  used to be a free retest; after the move it's a packer rebuild
  (15-30 min). Acceptable because pool layout changes are rare.

## Out of scope

- Hardware/topology changes to prod lab / pug (not a CI concern).
- Renaming `host_vars/lab.yml` / `pug.yml` (prod host_vars stay as-is).
- Anything touching `roles/`: this is purely test-infra plumbing.

## Implementation phases

Each phase = one commit. Run `mise run lint:ansible-changed` before each
commit; run `test/testrole.py <touched-role>` at relevant junctures.

### Phase 1 — Packer absorbs pool creation

Files: `packer/qemu.pkr.hcl`, `packer/scripts/provision.sh` (and possibly
`packer/scripts/pools.sh` if it grows too large to inline).

1. In `qemu.pkr.hcl`'s `variant_config`:
   - Add `box` source: 8 disks total (2 mirror rpool legs + 2 apoc + 6
     lab-shape extras), `layout = "mirror"`.
   - Rename `zfs` → `pug` (single rpool + 2 apoc disks).
   - Rename `zfs-lab` → `lab` (mirror rpool + 6 dozer/tank/mouse extras).
2. Add a new `source "qemu.ubuntu"` block for `box`.
3. In `provision.sh`, after rpool setup completes, dispatch on
   `$SOURCE_NAME` and run the apoc / dozer / tank / mouse `zpool create`
   commands lifted from `test/disks/{lab,pug}.sh`. Create with
   `cachefile=/etc/zfs/zpool.cache` (or import-then-export to seed
   `zfs-import-cache.service`) so the pools auto-import on boot.
4. Verify the final `mise run packer:build` produces all three variant
   artifact dirs with the expected disk counts.

### Phase 2 — Consolidate host_vars

Files: `host_vars/box.yml`, `host_vars/minimal.yml` (new), `hosts.ini`.
Delete: `host_vars/box-qemu.yml`, `host_vars/box-qemu-minimal.yml`,
`host_vars/lab-qemu.yml`, `host_vars/pug-qemu.yml`.

1. Merge `box-qemu.yml` keys into `box.yml`. Update `box.yml` to reflect
   the lab-prod-shape pool layout: `zfs_dozer_filesystem: dozer`,
   `zfs_tank_filesystem: tank`, ARC sizes from lab.yml, etc.
2. Add `qemu_test: true` and `zfs_has_*_mount` flags from the old
   `box-qemu.yml` directly into `box.yml`.
3. Rename `box-qemu-minimal.yml` → `minimal.yml`; add
   `qemu_test: true`, `qemu_test_minimal: true` directly.
4. `hosts.ini`: add `minimal` alongside `box` in `[test]`.
5. Decide on lab-qemu.yml / pug-qemu.yml: either delete (full retirement)
   or keep as-is for opt-in `testrole.py --machine lab/pug` local runs.
   **Recommend keep** (zero per-push cost, residual coverage; harness
   continues to know about lab/pug as targets).

### Phase 3 — Harness simplification

Files: `test/machine.py`.

1. In `QemuMachineSpec` table: minimal's `inventory_host` → `"minimal"`.
   Drop `extra_disks` and `disk_setup_script` fields (and supporting
   `run_disk_setup` method).
2. Update box spec to `packer_image="box"` (post-rename) and grow
   `os_disk_count` to total disk count (rpool + extras), since packer
   now ships them all.
3. Drop the `-qemu` suffix logic in `_qemu_ansible_args`. Replace with
   simple `--limit <inventory_host>`. Drop the `-e {"qemu_test":...}`
   JSON injection (those values now in host_vars).
4. Lab/pug specs: drop `disk_setup_script`, retain spec entries with
   `packer_image="lab"`/`packer_image="pug"` and full `os_disk_count`.

### Phase 4 — CI matrix drops lab/pug fan-out

Files: `mise-tasks/ci/detect-roles`, `.github/ci-lab-roles.txt`.

1. In `detect-roles`: remove the `LAB_ROLES` block (file + escalation
   logic). Keep `MINIMAL_ROLES` — the minimal escalation is still
   meaningful (preinstalled snapd, cloud-init, ext4).
2. Update the cross-cut regex: `host_vars/.*-qemu.*\.yml` →
   `host_vars/(box|minimal)\.yml` (since the qemu suffix is gone).
3. Delete `.github/ci-lab-roles.txt`.
4. Test-nightly.yml: unchanged — it already runs the full universe
   across all variants; that's where lab/pug coverage lives now.

### Phase 5 — Cleanup + docs

Files: `test/disks/` (delete), `CLAUDE.md`.

1. `rm -rf test/disks/`.
2. CLAUDE.md updates:
   - "Test Environment Design" — three packer variants, single CI
     fan-out target (box), lab/pug as packer-script regression + on-
     demand harness targets.
   - "Continuous Integration" — drop the lab-roles-escalation paragraph;
     keep minimal-roles.
   - Remove references to `test/disks/*.sh` and the
     `disk_setup_script` field.

## Open decisions to make during implementation

1. Drop or keep `host_vars/lab-qemu.yml` / `pug-qemu.yml`? Recommend
   keep — zero push-cost, retains `--machine lab/pug` local debug.
2. Where does the pool-creation logic live: extend `provision.sh`
   inline, or factor into `packer/scripts/pools.sh`? Decide based on
   `provision.sh` final size; inline is fine up to ~100 added lines.
3. Box swap + EFI: mirror like lab, or single like pug? Mirror, since
   box is supposed to be the lab-shape fixture.
