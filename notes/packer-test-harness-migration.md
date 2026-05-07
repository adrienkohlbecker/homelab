# Replace machine.py with a Packer-driven test harness

## Context

`test/machine.py` is 1051 lines of asyncio + qemu lifecycle code that boots disposable QEMU guests, waits for SSH, runs ansible playbooks, captures logs, and tears down on failure. Most of that machinery (boot/SSH-wait/ansible-cmd-formatting/timeout-escalation/UEFI-pflash/free-port-walking) is what Packer's qemu builder + ansible provisioner already do natively.

The goal of this migration is to move the qemu lifecycle into a single generic Packer template (`packer/test.pkr.hcl`) parameterised by variables, with a thin Python wrapper that builds the var dict and shells out to `packer build`. Net win: ~700 fewer lines of harness code, more declarative shape, fewer custom bugs (signal-escalation, port-band races, fact-cache wiring) to own.

In-scope:
- New packer template that boots all four variants (`minimal`, `box`, `lab`, `pug`) on x86_64 + aarch64
- New Python wrapper `test/runtest.py` replacing `test/testrole.py`
- Migrate `test/launch.py` to packer for default + `--exit-after-ready` modes; keep a small raw-qemu fallback for `--foreground` / `--no-ssh-wait` (packer can't drive qemu with the user's controlling tty in raw mode for mon:stdio)
- Slim `test/machine.py` from 1051 → ~200 lines for the launch.py fallback only
- `test/testall.py` swaps `testrole.py` → `runtest.py` and drops the PeakKB column
- Decisions: **keep idempotence** (parse PLAY RECAP from packer stdout), **drop peak-RSS** entirely, **migrate launch.py with a fallback**

## Files to create

### `packer/test.pkr.hcl` (~280 lines)

Single `source "qemu" "test"` parameterised by ~25 vars. Locals derive arch, accelerator, EFI paths (lifted from `packer/qemu.pkr.hcl:39-104`).

Variables (declared with sensible defaults so launch.py and runtest.py only have to set what they care about):

```
variable "ubuntu_name"          { type = string }
variable "arch"                 { type = string }                        # x86_64 | aarch64
variable "ssh_username"         { type = string }                        # ubuntu | vagrant
variable "iso_url"              { type = string }                        # abs path: vda source qcow2 OR cloud image
variable "iso_checksum"         { type = string default = "none" }
variable "extra_drive_files"    { type = list(string) default = [] }     # pre-staged vdb..vdN qcow2 (lab OS extras + variant extras)
variable "host_port_min"        { type = number }
variable "host_port_max"        { type = number }
variable "vnc_port_min"         { type = number }
variable "vnc_port_max"         { type = number }
variable "memory_mb"            { type = number default = 4096 }
variable "vcpus"                { type = number default = 8 }
variable "direct_boot_kernel"   { type = string default = "" }           # "" disables; abs path otherwise
variable "direct_boot_initrd"   { type = string default = "" }
variable "direct_boot_cmdline"  { type = string default = "" }
variable "use_efi"              { type = bool default = true }
variable "efi_vars_seed"        { type = string default = "" }           # packer-baked efivars.fd, or ""
variable "seed_iso"             { type = string default = "" }           # cidata for minimal; "" otherwise
variable "role"                 { type = string default = "" }           # role under test; "" disables ansible
variable "inventory_host"       { type = string }                        # box | lab | pug | minimal_box (use box host_vars)
variable "host_vars_suffix"     { type = string default = "" }           # "-minimal" or ""
variable "qemu_test_minimal"    { type = bool default = false }
variable "disk_setup_script"    { type = string default = "" }
variable "extra_disk_devices"   { type = list(string) default = [] }
variable "checkmode"            { type = bool default = true }
variable "idempotence"          { type = bool default = true }
variable "has_setup_hook"       { type = bool default = false }
variable "has_verify_hook"      { type = bool default = false }
variable "purge_snapd"          { type = bool default = false }
variable "upstream_mirrors"     { type = bool default = false }
variable "keep_vm"              { type = bool default = false }
variable "exit_after_ready"     { type = bool default = false }
variable "output_directory"     { type = string }
variable "ansible_extra_env"    { type = map(string) default = {} }
```

Provisioner ordering, each gated by a `count`-style toggle (use `dynamic` blocks where packer accepts them, otherwise `null` filtering via `local.provisioners`):

```
1.  shell                 disk setup script (gated on disk_setup_script != "")
                          file provisioner stages /tmp/disk_setup.sh first
2.  shell                 snapd purge       (gated on purge_snapd)
3.  ansible               test/playbooks/_mirrors.yml         (always when role != "")
4.  ansible               test/playbooks/_setup.yml           (gated on has_setup_hook)
5.  ansible               test/playbooks/site.yml --check     (gated on checkmode)
6.  ansible               test/playbooks/site.yml             (main apply)
7.  ansible               test/playbooks/site.yml             (idempotence rerun)
8.  ansible               test/playbooks/_verify.yml          (gated on has_verify_hook)
9.  shell (in-guest)      systemctl is-system-running --wait  (gated on exit_after_ready)
10. breakpoint            "VM ready, ssh -p ..."              (gated on keep_vm && !exit_after_ready)
```

`error-cleanup-provisioner` block: `shell` runs `journalctl --no-pager --priority info` to `/tmp/journal.log`, `file` provisioner downloads it as `${output_directory}/journal.ansi`.

Each ansible provisioner uses `extra_arguments` to pass:
- `--limit ${var.inventory_host}`
- `-e _role_under_test=${var.role}`
- `-e @host_vars/${var.inventory_host}-qemu${var.host_vars_suffix}.yml`
- `-e {"qemu_test":true,"qemu_test_minimal":${var.qemu_test_minimal}}`
- `-e nexus_url=` when `var.upstream_mirrors`

`use_proxy = false` so packer's ansible provisioner connects directly to the guest IP (matches today's machine.py behaviour). `inventory_file = "test/inventory.ini"` so `--limit` resolves against the static inventory containing `box`, `lab`, `pug`. The static playbooks at `test/playbooks/site.yml` etc. all use `hosts: all`, so the limit is what selects the box.

Locals lift directly from `packer/qemu.pkr.hcl`:
```hcl
qemu_binary       = "qemu-system-${var.arch}"
machine_type      = var.arch == "x86_64" ? "q35" : "virt"
accelerator       = var.arch == "x86_64" ? "kvm" : "hvf"
efi_code          = var.arch == "x86_64" ? "/usr/share/OVMF/OVMF_CODE.fd" : "/opt/homebrew/share/qemu/edk2-aarch64-code.fd"
efi_vars_blank    = var.arch == "x86_64" ? "/usr/share/OVMF/OVMF_VARS.fd"  : "/opt/homebrew/share/qemu/edk2-arm-vars.fd"
arch_qemuargs     = var.arch == "aarch64" ? [["-device","virtio-gpu-pci"],["-device","qemu-xhci"],["-device","usb-kbd"],["-device","usb-tablet"]] : []
```

`qemuargs` composition: base RNG + `extra_drive_files` as `-drive` entries + optional `seed_iso` + optional direct-boot triple + `arch_qemuargs`.

**Critical risk**: when direct-boot is active (aarch64 + ZFS variants), packer's `disk_image=true + use_backing_file=true` may conflict with manually-injected `-drive` entries via `qemuargs`. If it does, the wrapper pre-stages vda overlay too (one extra `qemu-img create -b`) and the template flips to `disk_image=false`, attaching every drive via `qemuargs`. Implementation should test the simple path first; fall back if qemu rejects duplicate drives.

### `test/runtest.py` (~280 lines)

Replaces `testrole.py`. Imports `VARIANTS` and `QemuLauncher` from the slimmed `machine.py`.

Function structure:
- `parse_args()`: CLI parity with testrole.py minus `--benchmark`, `--keep-logs` (testall.py drives log retention separately).
- `pick_free_port_band(start, count) -> (lo, hi)`: bind-test consecutive 127.0.0.1 ports; pass band to packer.
- `stage_inputs(spec, ubuntu_name, arch, upstream_mirrors) -> dict`: creates a tempdir next to imagedir, returns the full var dict — handles cloud-image download for `minimal`, OS-disk overlay pre-staging for `lab`'s vdb/vdc, direct-boot kernel/initrd/cmdline resolution + cmdline augmentation (lifts `_augment_kernel_cmdline` from machine.py), efivars copy for ZFS+x86_64, and `_extra_disk_devices` list.
- `write_var_file(vars: dict, path: Path)`: writes a `.pkrvars.json` file with quoted strings + native lists/bools, since `-var key=value` doesn't handle list types cleanly.
- `run_packer(template, varfile, output_file) -> tuple[int, list[str]]`: `subprocess.Popen(["packer","build","-var-file",varfile,template], stdout=PIPE)` with line-by-line tee to `test/out/<machine>.<ubuntu>.<role>.output.ansi` and to a list for in-memory recap parsing. `subprocess.run(timeout=args.timeout)` for the deadline.
- `parse_idempotence_recap(stdout: list[str]) -> int`: locates the last `PLAY RECAP` block in the captured stream and sums `changed=N` (re-uses `_RECAP_CHANGED_RE` from `testrole.py:145`). Returns 0 if no recap found (e.g., packer failed before reaching it).
- `main()`: stage → write var-file → run packer → on rc==0 and `--idempotence`, parse recap → if `changed > 0`, exit 125 → cleanup tempdir unless `--keep`.

Exit codes preserved: 0 ok, 1 ansible/packer failure, 124 timeout (subprocess.TimeoutExpired), 125 idempotence violation, 130 SIGINT.

### `packer/scripts/test_disk_setup.sh` (~6 lines)

Stub that `bash`s the role-specific disk setup script. The wrapper writes the per-variant script (e.g., `test/disks/lab.sh`) into the tempdir and the file provisioner uploads it to `/tmp/disk_setup.sh`; this stub just exec's it with the disk device args.

### `packer/scripts/test_journal_dump.sh` (~3 lines)

`journalctl --no-pager --priority info > /tmp/journal.log`. Invoked by error-cleanup-provisioner.

### `test/_constants.py` (~12 lines)

Shared constants extracted from machine.py: `UBUNTU_RELEASES`, `DEFAULT_UBUNTU`, `MACHINE_CHOICES`, `OUT_DIR`, `SSH_KEY = "packer/vagrant.key"`, `SSH_HOST = "127.0.0.1"`. Imported by `runtest.py`, `launch.py`, `testall.py`.

## Files to modify

### `test/machine.py` (1051 → ~200 lines)

Slim down to support launch.py's raw-qemu fallback only. Keep:
- `QEMU_MACHINE_SPECS` (rename to `VARIANTS`) — the per-variant `NamedTuple` table at lines 80-121
- `QemuLauncher` class (rename from `QemuMachine`): just `__init__`, `prepare`, `_boot_command`, `_create_overlay`, `_uefi_drives`, `_resolve_direct_boot`, `_augment_kernel_cmdline`, `_pick_vnc_display`, `stop` — everything to assemble a qemu cmdline and clean up

Delete:
- All ansible-related methods (`format_ansible_cmd`, `ansible_command`, `ansible_env`, `ansible_args`)
- All ssh-related methods (`format_ssh_cmd`, `format_scp_cmd`, `ssh_command`, `ensure_ssh`, `_ssh_banner_ready`, `_find_ssh_port`)
- All log-collection (`output_file`, `journal_file`, `boot_file`, `collect_journal`, `print_file_tail`, `cleanup_logs`)
- `_read_vm_hwm`, `peak_rss_kb`, `wrapper_timeout`, `WRAPPER_GRACE_SECONDS`
- `_ensure_minimal_cloudimg` — moves to `runtest.py` (only the test runner downloads cloud images)
- `run_disk_setup` — gone, packer drives this

### `test/launch.py` (424 → ~250 lines)

Two execution paths kept:
1. **Packer path** (default mode, `--exit-after-ready`): generates packer vars via `runtest.py:stage_inputs()` (factor it into a shared helper `test/_packer_vars.py`), invokes packer with `role=""` (skips all ansible provisioners) and `keep_vm=true`. The breakpoint provisioner is the wait. `--exit-after-ready` flips on the in-guest `systemctl is-system-running --wait` shell provisioner and disables breakpoint.
2. **Raw-qemu fallback** (`--foreground`, `--no-ssh-wait`): keeps the current `_LaunchMachine` (renamed to subclass `QemuLauncher`), `subprocess.Popen` for foreground mode, asyncio for the async non-foreground non-packer path. ZBM iteration with `--kernel/--initrd/--append` works through this fallback when SSH isn't expected.

CLI flags unchanged. Routing in `main()`:
```
if args.foreground or args.no_ssh_wait:
    return run_raw(args)        # current code path, slightly slimmed
return run_packer(args)         # new path
```

### `test/testall.py`

- Replace `from machine import …` with `from _constants import MACHINE_CHOICES, UBUNTU_RELEASES, DEFAULT_UBUNTU`.
- Replace `test/testrole.py` cmd dispatch with `test/runtest.py` (line ~268).
- Drop `_capture_peak_kb` async reader (lines 302, 347-364) and `PEAK_KB_SENTINEL_PREFIX` import.
- Drop `PeakKB` from `JOBLOG_FIELDS` (line 41 area) — out.tsv schema simplifies.
- Drop the per-job stdout drain that fed PEAK_KB; just `proc.communicate()` or stream to log directly.
- ~20 lines deleted total.

### `mise.toml` / `mise-tasks/test/role`

Any task that dispatches to `test/testrole.py` repoints to `test/runtest.py`. Per Phase 1 exploration there's no `mise.toml` test task today, but check `mise-tasks/test/` if it exists.

### `mise-tasks/packer/verify`

Unchanged in invocation (still `test/launch.py --machine X --ubuntu Y --timeout 300 --exit-after-ready`); behaviour preserved by the launch.py packer path.

## Files to delete

- `test/testrole.py` (378 lines) — replaced by `runtest.py`
- `test/setup_mitogen.py` (~80 lines) — packer's ansible provisioner runs ansible-playbook directly; mitogen's strategy plugin still loads via `ANSIBLE_STRATEGY=mitogen_linear` env var passed through `ansible_env_vars` (no symlink dance needed if ansible.cfg already has `strategy_plugins = $(uv run python -c '...')`-style discovery; verify during implementation)

Keep:
- `test/playbooks/{site,_mirrors,_setup,_verify}.yml` — 4 four-liners; let packer reference them in-place rather than regenerate
- `test/disks/{lab,pug}.sh` — uploaded by file provisioner, exec'd by shell provisioner
- `test/inventory.ini`, `host_vars/*-qemu*.yml`, `group_vars/test.yml` — unchanged

## Tradeoffs and known risks

- **Idempotence parsing depends on packer's stdout shape.** Packer's ansible provisioner streams ansible's output through its own logger but `PLAY RECAP` lines pass through verbatim. We strip ANSI by setting `ANSIBLE_FORCE_COLOR=0` in `ansible_env_vars`. Risk: if packer adds line prefixing per-provisioner (it does prepend `==> qemu.test:`), the regex `\bchanged=(\d+)` still matches because it's bare-word. Verify on first run with a known-idempotent role.
- **Direct-boot + packer's `disk_image=true` may conflict.** Plan: try the simple path first; if qemu rejects duplicate drives, fall back to wrapper pre-staging vda overlay too and `disk_image=false`. Adds 5-10 lines to runtest.py if needed.
- **launch.py grows a router.** Two paths (packer / raw-qemu) is slightly more code than one, but each path is small and the choice is explicit.
- **Fact cache lost.** machine.py wired `ANSIBLE_FACT_CACHING_CONNECTION=$workdir/facts` so the ~5 ansible-playbook invocations shared facts. With packer, each ansible provisioner is a separate ansible-playbook run; pointing them at a shared dir under `var.output_directory` via `ansible_env_vars` restores this. Implement; cost is a `${packer_workdir}/facts` path string.
- **Mitogen.** `setup_mitogen.py` currently writes a symlink `.ansible-mitogen-strategy` pointing at the mitogen install. With packer it can be set via `ANSIBLE_STRATEGY=mitogen_linear` + `ANSIBLE_STRATEGY_PLUGINS=<path-to-mitogen>` env vars in `ansible_env_vars`. Verify mitogen still loads cleanly.
- **Packer's qemu output qcow2 leaks if not cleaned.** Packer's qemu builder always writes its build artifact to `output_directory`. The wrapper points it at the per-run tempdir and `tempfile.TemporaryDirectory` cleans it up. `--keep` keeps the tempdir for the user's session.

## Code reduction estimate

| File                          | Before | After  | Δ      |
| ----------------------------- | -----: | -----: | -----: |
| `test/machine.py`             |  1 051 |   ~200 |   −851 |
| `test/testrole.py`            |    378 |      0 |   −378 |
| `test/setup_mitogen.py`       |    ~80 |      0 |    −80 |
| `test/launch.py`              |    424 |   ~250 |   −174 |
| `test/testall.py`             |   ~600 |  ~−20  |    −20 |
| `packer/test.pkr.hcl`         |      0 |   ~280 |   +280 |
| `test/runtest.py`             |      0 |   ~280 |   +280 |
| `test/_constants.py`          |      0 |    ~12 |    +12 |
| `test/_packer_vars.py`        |      0 |    ~80 |    +80 |
| `packer/scripts/test_*.sh`    |      0 |    ~10 |    +10 |
| **Net**                       |        |        | **−841** |

Roughly cuts the harness footprint by 1/3 and replaces ~1 500 lines of Python qemu lifecycle code with a declarative HCL template + a thin orchestrator.

## Verification plan

Implement and verify in this order — each step gates the next:

1. **`packer/test.pkr.hcl` boots minimal on x86_64** with no provisioners (`role=""`). `packer build` exits 0, qcow2 is created and cleaned. Validates source declaration, port walking, EFI fallback.
2. **Add ansible provisioner** for `_mirrors.yml`. Run against `box` variant. Validates `use_proxy=false` + extra_arguments path + host_vars loading.
3. **`test/runtest.py --machine box test_box_role`** end-to-end. Validates the full provisioner chain (mirrors → site → idempotence rerun → verify) against a known-idempotent role.
4. **`test/runtest.py --machine pug zfs`** — exercises disk_setup_script + extra disks.
5. **`test/runtest.py --machine lab zfs`** — exercises 3 OS-disk pre-staging (most likely failure mode; this is where the direct-boot/disk_image conflict could surface).
6. **`test/runtest.py --machine minimal --no-checkmode --no-idempotence dummy`** — cloud-image path, snapd purge, ext4.
7. **Force a failing role** (insert `fail:` task); verify error-cleanup-provisioner downloads `journal.ansi` and packer exits non-zero.
8. **`test/runtest.py --machine box --keep test_box_role`** — breakpoint pauses; SSH into VM; resume; packer cleanly shuts down.
9. **Run `test/testall.py --jobs 5`** against the existing role matrix. out.tsv validates without `PeakKB`. Compare overall runtime against pre-migration baseline (expect parity).
10. **`test/launch.py --machine minimal`** (packer path). **`test/launch.py --machine box --foreground`** (raw-qemu fallback path). **`mise run packer:verify zfs`** still passes.
11. **aarch64 (Mac arm64)**: re-run steps 4 + 5 — validates direct-boot via `qemuargs` injection on ZFS variants.
12. Run `test/unit/test_qemu_boot_command.py` against the slimmed `machine.py` to confirm launch.py's raw-qemu path still assembles correct cmdlines.

## Critical files referenced

- `/Users/ak/Work/homelab/test/machine.py` — to slim
- `/Users/ak/Work/homelab/test/testrole.py` — to delete (replaced)
- `/Users/ak/Work/homelab/test/launch.py` — to refactor (router + packer path + raw fallback)
- `/Users/ak/Work/homelab/test/testall.py` — to modify (drop PeakKB)
- `/Users/ak/Work/homelab/packer/qemu.pkr.hcl` — locals to lift (`:39-104`)
- `/Users/ak/Work/homelab/test/playbooks/{site,_mirrors,_setup,_verify}.yml` — keep
- `/Users/ak/Work/homelab/test/disks/{lab,pug}.sh` — keep, uploaded via file provisioner
- `/Users/ak/Work/homelab/test/inventory.ini` — keep
- `/Users/ak/Work/homelab/host_vars/*-qemu*.yml` — keep
- `/Users/ak/Work/homelab/mise-tasks/packer/verify` — invocation unchanged
