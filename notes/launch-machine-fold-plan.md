# Fold `_LaunchMachine` into `QemuMachine` — Plan

## Summary

`test/launch.py` currently subclasses `QemuMachine` only to inject behaviour into four hooks (`_uefi_drives`, `_resolve_direct_boot`, `prepare`, `_boot_command`). All four are already on `QemuMachine`, and there is no shared state between launch.py's overrides and the rest of the codebase — the subclass is pure MRO plumbing. We fold every knob back into `QemuMachine.__init__` as keyword-only kwargs that default to `None`/`False`, delete the subclass, and reduce `launch.py` to argparse + a single `QemuMachine(...)` construction + a small foreground-lifecycle helper.

Existing callers (`/Users/ak/Work/homelab/test/testrole.py`, `/Users/ak/Work/homelab/test/unit/conftest.py`) keep their fixed-shape kwargs unchanged — none of the new kwargs are required, and behavioural change for the no-flag path is zero.

`_run_foreground` stays in `launch.py` (Option A, see §4) — pulling it onto `QemuMachine` would conflate the async test driver path with an interactive-only sync lifecycle.

---

## API delta

New kwargs added to `QemuMachine.__init__` (keyword-only, all default to `None`/`False`/`""`):

| Kwarg | Type | Default | Consumed by | Purpose |
|---|---|---|---|---|
| `kernel` | `Path \| None` | `None` | `prepare` (sets `_direct_boot_override` → `_direct_boot`) | Override packer-shipped kernel for direct-boot. |
| `initrd` | `Path \| None` | `None` | same | Override packer-shipped initrd. |
| `append` | `str` | `""` | same | Cmdline paired with `kernel`/`initrd`; ignored when `kernel is None`. |
| `mem` | `str \| None` | `None` | `_boot_command` (replaces value at `-m` slot) | Override `-m` (e.g. `"8G"`); `None` keeps per-spec `memory_mb`. |
| `with_pflash` | `bool` | `False` | `prepare` (post-super pflash attachment) | Force-attach UEFI pflash on variants that don't get it from super. |
| `efi_code` | `Path \| None` | `None` | `_uefi_drives` (overrides auto-detected blob); implies `with_pflash` in `prepare` | Custom OVMF/EDK2 CODE blob. |
| `efi_vars` | `Path \| None` | `None` | `_uefi_drives`; same implies-pflash | Custom EFI vars file. |
| `virtfs` | `list[tuple[Path, str]] \| None` | `None` (normalised to `[]`) | `_boot_command` (appends `-virtfs local,...` per entry) | 9p host-share specs. |
| `foreground` | `bool` | `False` | `_boot_command` (strips `timeout` prefix; swaps `-serial stdio` → `-serial mon:stdio`) | Inherit qemu stdio for HMP via Ctrl-A,c. |
| `qmp_socket` | `Path \| None` | `None` | `_boot_command` (appends `-qmp unix:...,server,nowait`) | Bind QMP to a unix socket. |

### New `QemuMachine.__init__` signature

```python
def __init__(
    self,
    machine: str,
    role: str,
    keep_vm: bool,
    ubuntu_name: str,
    machine_timeout: int,
    upstream_mirrors: bool = False,
    *,
    kernel: Path | None = None,
    initrd: Path | None = None,
    append: str = "",
    mem: str | None = None,
    with_pflash: bool = False,
    efi_code: Path | None = None,
    efi_vars: Path | None = None,
    virtfs: list[tuple[Path, str]] | None = None,
    foreground: bool = False,
    qmp_socket: Path | None = None,
) -> None:
```

Force-keyword (`*`) so positional callers can't accidentally collide. `testrole.py` and `conftest.py` pass kwargs only, so they're unaffected.

In the body, mirror the current `_LaunchMachine.__init__` attribute assignments:

```python
self._direct_boot_override: tuple[Path, Path, str] | None = (
    (kernel.resolve(), initrd.resolve(), append) if kernel is not None else None
)
self._mem = mem
self._with_pflash = with_pflash
self._efi_code = efi_code.resolve() if efi_code is not None else None
self._efi_vars = efi_vars.resolve() if efi_vars is not None else None
self._virtfs: list[tuple[Path, str]] = list(virtfs or [])
self._foreground = foreground
self._qmp_socket = qmp_socket
```

`(kernel is None) != (initrd is None)` validation stays in `launch.py:parse_args` — it belongs to the CLI parser, not `__init__`.

---

## Method-by-method walkthrough

### 1. `_uefi_drives`

Today's `QemuMachine._uefi_drives` does auto-detect + packer-vars-or-empty. The override only adds two short-circuits.

**New inline body** (replaces `/Users/ak/Work/homelab/test/machine.py:878-907`):

```python
async def _uefi_drives(self) -> list[str]:
    code_path = self._efi_code if self._efi_code is not None else uefi_code_path_for(self.arch)
    if self._efi_vars is not None:
        vars_path = self._efi_vars
    else:
        packer_vars = Path(f"{self.workdir.name}/efivars.fd")
        if packer_vars.exists():
            vars_path = packer_vars
        else:
            vars_path = Path(f"{self.workdir.name}/uefi-vars.fd")
            await run_command(["truncate", "-s", str(code_path.stat().st_size), str(vars_path)])
    return [
        f"file={code_path},if=pflash,unit=0,format=raw,readonly=on",
        f"file={vars_path},if=pflash,unit=1,format=raw",
    ]
```

When both override fields are `None`, this reduces exactly to today's parent — same packer-vars preference, same `truncate` sizing.

### 2. `_resolve_direct_boot`

**New inline body** (extends `/Users/ak/Work/homelab/test/machine.py:805-818`):

```python
async def _resolve_direct_boot(self, os_src_paths: list[str]) -> tuple[Path, Path, str]:
    if self._direct_boot_override is not None:
        return self._direct_boot_override
    image_dir = Path(os_src_paths[0]).parent
    kernel = image_dir / "kernel"
    initrd = image_dir / "initrd"
    cmdline = (image_dir / "cmdline").read_text().strip()
    return kernel, initrd, cmdline
```

### 3. `prepare`

Append the override-routing + conditional pflash attach at the end of the existing `QemuMachine.prepare` body (after `/Users/ak/Work/homelab/test/machine.py:803`):

```python
# Existing body unchanged (workdir, ssh_port, vnc_display, minimal branch,
# packer overlays, _direct_boot from _resolve_direct_boot, etc.)

# x86_64 / minimal don't call _resolve_direct_boot, so route the override
# in here too. On aarch64 ZFS this is a re-assignment of the same value.
if self._direct_boot_override is not None:
    self._direct_boot = self._direct_boot_override

# Attach pflash on variants that don't already have it (aarch64 ZFS
# direct-boot, x86_64 minimal BIOS). Idempotent: skip when an earlier
# branch already attached pflash (x86_64 ZFS, aarch64 minimal).
want_pflash = self._with_pflash or self._efi_code is not None or self._efi_vars is not None
if want_pflash and not any("if=pflash" in d for d in self.drives):
    self.drives += await self._uefi_drives()
```

No-flag path: `_direct_boot_override is None` (skip), `want_pflash` is `False` (skip). Identical to today.

### 4. `_boot_command`

**Option A (chosen): inline post-process at the end of the existing builder.** Smallest change, keeps `/Users/ak/Work/homelab/test/unit/test_qemu_boot_command.py` passing without touching it (no-flag construction → no-op post-process).

```python
def _boot_command(self) -> list[str]:
    # ... existing builder body unchanged, returns cmd list ...
    cmd = [...]

    if self._mem is not None:
        cmd[cmd.index("-m") + 1] = self._mem

    if self._foreground:
        if cmd[0] != "timeout":
            raise RuntimeError(f"expected timeout wrapper, got {cmd[:4]}")
        cmd = cmd[3:]
        cmd[cmd.index("-serial") + 1] = "mon:stdio"

    if self._qmp_socket is not None:
        cmd += ["-qmp", f"unix:{self._qmp_socket},server,nowait"]
    for path, tag in self._virtfs:
        cmd += [
            "-virtfs",
            f"local,id={tag},path={path},mount_tag={tag},security_model=mapped-xattr",
        ]
    return cmd
```

**Option B (rejected): split into `_post_process_boot_command(cmd)` hook.** Adds a method that's only ever called once, internally. No external caller needs the seam.

The defensive `RuntimeError` on a missing `timeout` prefix is preserved — same intent (fail loudly if the wrapper layout changes).

### 5. `_augment_kernel_cmdline` — semantics preserved

`_augment_kernel_cmdline` runs in `_boot_command` whenever `self._direct_boot is not None`. After the fold, `self._direct_boot` is set in `prepare` to either the override tuple (with the user's `append` string) or the packer-resolved tuple — same as today. **The user's `--append` already flows through augmentation today**: `_LaunchMachine.prepare` writes the override into `self._direct_boot`, then `super()._boot_command()` (parent) is what calls `_augment_kernel_cmdline`. The `--append` help text in `/Users/ak/Work/homelab/test/launch.py:222` documents this explicitly.

The augmentation is conditional on `serial_console_token not in cmdline` (and `console=tty0` when `keep_vm`), so a user-supplied `console=ttyAMA0` doesn't get a duplicate. Locked in by `test_direct_boot_aarch64_does_not_duplicate_existing_ttyAMA` in `/Users/ak/Work/homelab/test/unit/test_qemu_boot_command.py`.

**No semantic change after the fold.**

---

## Where `_run_foreground` lives

**Option A (chosen): keep `_run_foreground` in `launch.py` as a function taking `m: QemuMachine`.** Two-line signature change vs. today.

The foreground lifecycle has three pieces that don't generalise:
1. `asyncio.run(m.prepare())` — short-lived loop for IO-bound prep.
2. `subprocess.Popen(m._boot_command())` + `proc.wait()` — sync, qemu owns the controlling tty.
3. `m.workdir.cleanup()` in `finally` — manual stand-in for the unused `__aexit__`.

Reasons not to put `run_foreground()` on `QemuMachine` (Option B):
- Would be the only sync method on the class; everything else is async.
- `subprocess.Popen` shape conflicts with the rest of `QemuMachine`'s `asyncio.create_subprocess_exec` discipline.
- Tempts future code into using it outside the foreground mon:stdio case, but it exists *only* because of the qemu+asyncio raw-tty bug — not a general-purpose entry point.

The launcher already pokes `m._boot_command()` directly; one more `_*` poke (`m.workdir`) for the foreground cleanup is honest about "the launcher bypasses `__aenter__`".

---

## Idempotency / no-op cases (no-flag path = today's behaviour)

| Invariant | Why |
|---|---|
| `prepare` post-block is a no-op. | `_direct_boot_override is None`; `_with_pflash` / `_efi_code` / `_efi_vars` all falsy → `want_pflash is False`. |
| `_uefi_drives` returns same blobs as today. | Both short-circuits take the `else` arm. |
| `_boot_command` post-process is a full no-op. | `_mem is None`, `_foreground is False`, `_qmp_socket is None`, `_virtfs == []`. |
| `_resolve_direct_boot` returns the packer tuple. | Override guard `_direct_boot_override is None` is True. |

`/Users/ak/Work/homelab/test/unit/test_qemu_boot_command.py` constructs via `qemu_machine_factory` (no new kwargs) and asserts cmdline shape on x86_64/aarch64 × keep_vm × direct-boot. **Should pass unchanged.**

---

## Sketch of new `launch.py`

```python
#!/usr/bin/env -S uv run
"""Launch a QEMU machine via the test harness driver, no role/ansible."""

import argparse
import asyncio
import contextlib
import signal
import subprocess
import sys
from pathlib import Path

from machine import (
    DEFAULT_UBUNTU,
    QEMU_MACHINE_SPECS,
    QemuMachine,
    UBUNTU_RELEASES,
)
from utils import cancel_on_signal, print_cmd_line, print_line, tee_output


def _parse_virtfs(specs: list[str]) -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for spec in specs:
        path, sep, tag = spec.rpartition(":")
        if not sep or not path or not tag:
            raise argparse.ArgumentTypeError(f"--virtfs expects PATH:TAG, got {spec!r}")
        out.append((Path(path).resolve(), tag))
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--machine", default="minimal", choices=sorted(QEMU_MACHINE_SPECS))
    parser.add_argument("--ubuntu", default=DEFAULT_UBUNTU, choices=sorted(UBUNTU_RELEASES))
    parser.add_argument("--timeout", type=int, default=0, metavar="SECONDS")
    parser.add_argument("--kernel", type=Path)
    parser.add_argument("--initrd", type=Path)
    parser.add_argument("--append", default="")
    parser.add_argument("--mem", default=None, metavar="SIZE")
    parser.add_argument("--with-pflash", action="store_true")
    parser.add_argument("--efi-code", type=Path, default=None, metavar="PATH")
    parser.add_argument("--efi-vars", type=Path, default=None, metavar="PATH")
    parser.add_argument("--virtfs", action="append", default=[], metavar="PATH:TAG")
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--qmp", type=Path, default=None, metavar="SOCKET")
    parser.add_argument("--no-ssh-wait", action="store_true")
    parser.add_argument("--exit-after-ready", action="store_true")
    # (preserve full help= strings from current launch.py)

    args = parser.parse_args()
    if (args.kernel is None) != (args.initrd is None):
        parser.error("--kernel and --initrd must be provided together")
    if args.exit_after_ready and (args.foreground or args.no_ssh_wait):
        parser.error("--exit-after-ready requires waiting for SSH")
    try:
        args.virtfs = _parse_virtfs(args.virtfs)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    return args


async def _run_async(m: QemuMachine, *, wait_for_ssh: bool, exit_after_ready: bool) -> None:
    # Unchanged from current launch.py (~30 lines).
    ...


def _run_foreground(m: QemuMachine) -> int:
    try:
        asyncio.run(m.prepare())
        cmd = m._boot_command()
        print_cmd_line(cmd)
        proc = subprocess.Popen(cmd)
        try:
            return proc.wait() or 0
        except KeyboardInterrupt:
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGTERM)
            proc.wait()
            return 130
    finally:
        m.workdir.cleanup()


def main() -> int:
    args = parse_args()
    m = QemuMachine(
        machine=args.machine,
        role="_launch",
        keep_vm=True,
        ubuntu_name=args.ubuntu,
        machine_timeout=args.timeout,
        kernel=args.kernel,
        initrd=args.initrd,
        append=args.append,
        mem=args.mem,
        with_pflash=args.with_pflash,
        efi_code=args.efi_code,
        efi_vars=args.efi_vars,
        virtfs=args.virtfs,
        foreground=args.foreground,
        qmp_socket=args.qmp,
    )

    if args.foreground:
        return _run_foreground(m)

    rc = 0
    try:
        with tee_output(m.output_file):
            asyncio.run(_run_async(m,
                                   wait_for_ssh=not args.no_ssh_wait,
                                   exit_after_ready=args.exit_after_ready))
    except asyncio.CancelledError:
        print_line("\nInterrupted, shutting down...")
        rc = 130
    except KeyboardInterrupt:
        rc = 130
    return rc


if __name__ == "__main__":
    sys.exit(main())
```

Drops `_LaunchMachine` (~95 lines), drops `uefi_code_path_for` import. Final file ~120-140 LOC with full help= strings preserved (recommended); ~95 LOC if help strings are collapsed (separate concern).

---

## Verification steps

In order — each isolates one slice:

1. **Unit suite, base API.** `pytest test/unit/`. Locks "no new kwarg = no behavioural change".
2. **testrole asyncio path.** `test/testrole.py systemd --machine minimal --no-checkmode --no-idempetence` (or any quick role). Catches kwarg-collisions / import regressions in production path.
3. **Launcher, no flags.** `test/launch.py --machine minimal --no-ssh-wait`. Boots, prints "Booted", drops to `m.wait()`, Ctrl-C → 130.
4. **Foreground / mon:stdio.** `test/launch.py --machine box --foreground --no-ssh-wait`. qemu inherits stdio; Ctrl-A,c → HMP, Ctrl-A,x → quit. Most regression-prone.
5. **Direct-boot override.** `test/launch.py --machine box --kernel <p> --initrd <p> --append 'foo bar' --no-ssh-wait`. Verify `print_cmd_line` shows `-kernel/-initrd/-append` with augmented cmdline; on aarch64 ZFS no `if=pflash`; on x86_64 minimal no `if=pflash`.
6. **virtfs.** `test/launch.py --machine box --virtfs /tmp:hosttmp --no-ssh-wait`; SSH in and `mount -t 9p hosttmp /mnt`.
7. **--with-pflash on aarch64 ZFS.** `test/launch.py --machine box --with-pflash --no-ssh-wait`. Pflash pair appended despite direct-boot — guard correctly sees no `if=pflash` in `self.drives`.
8. **--mem.** `test/launch.py --machine minimal --mem 1G --no-ssh-wait`; printed cmd has `-m 1G`.
9. **Exit-after-ready regression.** `test/launch.py --machine minimal --exit-after-ready` on x86_64. Confirms asyncio path still wires through.

If 1, 2, 3 pass cleanly, the fold is mechanically correct.

---

## Estimated LOC delta

| File | Current | After | Delta |
|---|---|---|---|
| `/Users/ak/Work/homelab/test/launch.py` | 425 | ~125 | **−300** |
| `/Users/ak/Work/homelab/test/machine.py` | 1052 | ~1100 | **+48** |
| **Net** | | | **−250** |

`machine.py` growth: ~20 lines `__init__` (kwargs + assignments), ~3 lines `prepare` (override-fold + pflash conditional), ~3 lines `_resolve_direct_boot`, ~10 lines `_uefi_drives`, ~12 lines `_boot_command` post-process.

`launch.py` shrink: ~95 lines `_LaunchMachine` class + docstring, ~3 lines imports, rest is whitespace and verbose argparse `help=` strings (recommend keeping them — primary user-facing flag docs).

---

## What this plan deliberately does NOT do

- **No new abstraction layer.** `_virtfs` stays `list[tuple[Path, str]]`, not a dataclass; `_qmp_socket` stays a `Path`. No "device plugin system".
- **No async `run_foreground` on the class.** Keeps the asyncio entry surface clean.
- **No `_post_process_boot_command` hook.** Inlining is shorter and clearer.
- **No change to `_augment_kernel_cmdline` semantics.** User's `--append` already flows through augmentation today; preserved verbatim.
- **No relocation of paired-flag validation** into `__init__` — belongs to CLI parser.
