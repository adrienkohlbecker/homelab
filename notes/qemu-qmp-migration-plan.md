# QMP migration plan for the test harness

## Summary

Today's qemu lifecycle in `test/machine.py` rests on three loosely-coupled mechanisms:

1. `-pidfile` + a polling loop in `Machine.ensure_booted` (line ~435) to detect "qemu is up".
2. `proc.returncode` checks to detect "qemu died early".
3. `terminate_pid(pid, grace_seconds=5)` (SIGTERM-then-SIGKILL on the qemu pid) plus a kernel-level `timeout --kill-after=10s WRAPPER_TIMEOUT` shell wrapper in `_boot_command()` as a last-resort backstop.

Each layer is racy in isolation. None of them tells us whether qemu has finished initial setup or accepted any guest input — only that the process exists. Graceful shutdown is via SIGTERM (qemu maps it to ACPI power-button); there's no acknowledgement, just a 5 s wait then SIGKILL.

The proposal: add a harness-owned QMP control socket, connect after spawn, and replace boot detection / early-death detection / graceful shutdown with QMP. Specifically:

- **Boot detection**: `await QMPClient.connect(sock)`. The greeting handshake completes only once qemu has reached its main loop — stronger than pidfile presence.
- **Early death**: `ConnectError` on connect, or `EOFError` / `DisconnectedError` mid-test.
- **Graceful shutdown**: `system_powerdown`, await `SHUTDOWN` event with timeout, fall back to `quit`, then SIGKILL the wrapper subtree if even `quit` doesn't land.
- **Peak RSS**: unchanged. Keep `-pidfile` for the sole purpose of feeding `/proc/<pid>/status` on Linux. QMP has no host-process-level VmHWM equivalent (it queries *guest* memory state).
- **`timeout` wrapper**: keep. It still gates the outermost lifetime and is what makes `--keep --timeout 0` work.
- **launch.py `--qmp`**: separate socket from the harness's. Don't share.

Net LOC change is roughly +30 (~+50 / −20). The real win is qualitative: three race-prone polling loops collapse to one event-driven shutdown with a structured exception model.

## Library survey: `qemu.qmp`

- **PyPI name**: `qemu.qmp` (note the dot — namespaces under the `qemu` umbrella). Latest version on PyPI: **0.0.6**. The unrelated `qmp` package on PyPI should not be confused with this one.
- **Upstream repo**: https://gitlab.com/qemu-project/python-qemu-qmp (maintained by John Snow / Red Hat).
- **Docs**: https://qemu.readthedocs.io/projects/python-qemu-qmp/en/latest/
- **License**: dual `GPL-2.0-only AND LGPL-2.0-or-later` (same as qemu itself). Compatible with internal-test use.
- **Maintenance**: actively maintained — recent work in 2024–2025 includes Python 3.13/3.14 support, Fedora Rawhide packaging fixes, avocado→pytest test migration. Issues tracker on GitLab is responsive.
- **Python version**: 3.8+; we run 3.12+ via mise so fine.
- **Dependencies**: zero hard runtime deps. Pure Python over `asyncio`.
- **Async API**: `qemu.qmp.QMPClient` — fully `asyncio`-native, the API we want. `qemu.qmp.legacy` provides a sync facade we don't want.
- **Capability negotiation**: handled transparently. `QMPClient(name, negotiate=True)` (the default) makes `connect()` await the greeting and send `qmp_capabilities` automatically before returning. We don't write any of the handshake.
- **Mac arm64**: pure Python over a unix socket; works wherever qemu's `-qmp unix:` works.
- **Exception types** (all from `qemu.qmp.error.QMPError`):
  - `ConnectError` — `connect()` failed; wraps a root-cause via `__cause__`.
  - `NegotiationError` — subclass of `ConnectError` for the post-greeting phase.
  - `StateError` — operation called at the wrong time (e.g. connecting twice).
  - `ExecuteError` — server returned `{"error": ...}` for our command.
  - `DisconnectedError` — peer hung up mid-stream.
  - Plain `EOFError` / `OSError` propagate through the listener task when the socket dies.

### Core API surface we'll use

```python
from qemu.qmp import QMPClient

qmp = QMPClient(name="homelab-box-zfs")
await qmp.connect("/path/to/qmp.sock")          # negotiates capabilities
res = await qmp.execute("query-status")          # returns dict
await qmp.execute("system_powerdown")            # returns {} on success
async with qmp.listener(("SHUTDOWN",)) as listener:
    async for event in listener:
        ...
await qmp.disconnect()
```

`execute()` raises `ExecuteError` on a server-side error — exactly what we want, no manual JSON inspection.

### QMP commands we'll use

- `query-status` — `{"running": bool, "status": str}`. Cheap health probe.
- `query-name` — returns the `-name` we passed; sanity check on the right qemu.
- `system_powerdown` — ACPI power-button press. Async: command acks immediately, actual shutdown lands later via guest cooperation.
- `quit` — terminate qemu without graceful guest cleanup. Always succeeds.
- `SHUTDOWN` event — emitted when qemu is about to exit; carries `guest: bool` distinguishing guest-initiated from `quit`-initiated.

There is **no `query-pid` QMP command** in upstream qemu (verified against QAPI schema and the QMP reference). So `-pidfile` stays in `_boot_command()` and `_read_vm_hwm` keeps working as today.

## Current vs proposed lifecycle

### Current boot path

```
Machine.boot()
  └─ asyncio.create_subprocess_exec("timeout 60 qemu-system-... -pidfile pid -serial stdio")
        └─ qemu writes pid file at some point during early init
Machine.ensure_booted()
  └─ loop:
       if proc.returncode is not None: raise RuntimeError("Launching machine failed")
       if pid file exists: break
       if deadline: raise TimeoutError
       await sleep_tick()  # 1 s
```

Caveats:
- "pid file exists" fires as soon as qemu calls `qemu_create_pidfile()`, which happens during early init. SSH banner readiness comes minutes later — `ensure_ssh()` covers that, so the gap is fine in practice but "booted" is a misnomer.
- `proc.returncode` is the only signal that qemu died early, and it requires the wrapper's process group to actually exit.

### Proposed boot path

```
Machine.boot()
  └─ asyncio.create_subprocess_exec("timeout <T> qemu-system-... -pidfile pid -qmp unix:qmp.sock,server,nowait -serial stdio")
        ├─ qemu starts listening on qmp.sock (just before the main loop)
        └─ qemu writes pid file
Machine.ensure_booted()
  └─ deadline-bounded retry around qmp.connect(qmp.sock):
       (a) FileNotFoundError       → socket not yet created, sleep_tick + retry
       (b) ConnectionRefusedError  → file present but qemu not yet listen()ing, retry
       (c) ConnectError / NegotiationError → qemu died after listen() but before greeting; bail
       (d) success                 → optional query-name probe
     in parallel: if proc.returncode is not None, bail (covers the "execve failed / panicked early" case)
```

Two things still need polling because qemu's QMP listener isn't present at process start: the socket file must exist *and* qemu must have called `listen()` on it. Both happen well before sshd, so we don't race the rest of the harness.

### Current shutdown path (`QemuMachine.stop`, line ~1018)

```
read pid from pidfile
peak_rss_kb = _read_vm_hwm(pid)        # /proc/<pid>/status, Linux-only
asyncio.shield(terminate_pid(pid, grace_seconds=5))
   └─ os.kill(pid, SIGTERM)            # qemu interprets as ACPI power
   └─ poll os.kill(pid, 0) every 200 ms
   └─ os.kill(pid, SIGKILL) after 5 s
super().stop()                          # wait for `timeout` wrapper to drain, kill if it doesn't
```

### Proposed shutdown path

```
peak_rss_kb = _read_vm_hwm(pid_from_pidfile)   # unchanged
async with self.qmp.listener(("SHUTDOWN",)) as shutdown:
    await self.qmp.execute("system_powerdown")
    try:
        async with asyncio.timeout(GRACEFUL_SHUTDOWN_SECONDS):  # 15 s
            await shutdown.get()                                # SHUTDOWN event
    except TimeoutError:
        with contextlib.suppress(qemu.qmp.QMPError):
            await self.qmp.execute("quit")                       # force exit
await self.qmp.disconnect()
super().stop()                                                   # 5 s wait, then SIGKILL the wrapper subtree
```

`terminate_pid` is no longer in this path — the harness no longer holds a bare pid for qemu, only `self.proc` (the `timeout` wrapper). `terminate_pid` stays in `utils.py` for any other code that needs it.

The block is wrapped in `asyncio.shield()` exactly like today so a second Ctrl-C mid-cleanup doesn't strand qemu.

## API mapping table

| Concern | Today | Proposed |
| --- | --- | --- |
| Socket / channel | `-pidfile` written by qemu; SIGTERM on `pid`. | `-qmp unix:{workdir}/qmp.sock,server,nowait` plus existing `-pidfile` (kept for `_read_vm_hwm`). |
| "qemu is up" | `Path("{workdir}/pid").exists()` poll. | `await QMPClient.connect(qmp.sock)` (handshake completes). |
| "qemu died early" | `proc.returncode is not None` while polling pidfile. | `proc.returncode is not None` *or* `ConnectError` on connect. |
| Health probe | none | `await qmp.execute("query-status")` (optional). |
| Graceful shutdown | `os.kill(pid, SIGTERM)` (qemu maps to ACPI). | `await qmp.execute("system_powerdown")` then await `SHUTDOWN` event with timeout. |
| Hard shutdown | `os.kill(pid, SIGKILL)` after 5 s grace. | `await qmp.execute("quit")` first; SIGKILL wrapper subtree if even that doesn't land. |
| Peak host RSS | `/proc/<pid>/status:VmHWM` via `_read_vm_hwm`. | Unchanged. QMP has no host-side equivalent; pid still from `-pidfile`. |
| Last-resort kernel kill | `timeout --kill-after=10s WRAPPER_TIMEOUT` shell wrapper. | Same. |

## Concrete code sketches

State on `QemuMachine`:

```python
class QemuMachine(Machine):
    qmp: QMPClient | None = dataclasses.field(default=None, init=False)
    qmp_socket_path: Path = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.qmp_socket_path = Path(self.workdir.name) / "qmp.sock"
```

`_boot_command()` adds (after `-serial stdio`):

```python
"-qmp", f"unix:{self.qmp_socket_path},server,nowait",
```

`nowait` is critical — without it qemu blocks at startup until a client connects, deadlocking our spawn-then-connect ordering.

`ensure_booted()` overridden on `QemuMachine`:

```python
async def ensure_booted(self) -> None:
    deadline = time.monotonic() + IDFILE_TIMEOUT
    self.qmp = QMPClient(f"homelab-{self.machine}-{self.role}")
    while True:
        if self.proc and self.proc.returncode is not None:
            raise RuntimeError("Launching machine failed")
        if time.monotonic() > deadline:
            raise TimeoutError(f"QMP socket {self.qmp_socket_path} not ready within {IDFILE_TIMEOUT}s")
        try:
            await self.qmp.connect(str(self.qmp_socket_path))
            break
        except (FileNotFoundError, ConnectionRefusedError):
            # Socket file not yet created, or qemu not yet in accept loop.
            await sleep_tick()
        except qemu.qmp.ConnectError as exc:
            # Negotiation failed: qemu came up far enough to listen() but
            # died before/during the greeting. Treat like proc death.
            raise RuntimeError(f"QMP negotiation failed: {exc}") from exc
    await self.qmp.execute("query-status")  # cheap sanity probe
```

`stop()`:

```python
GRACEFUL_SHUTDOWN_SECONDS = 15  # ACPI shutdown is genuinely slower than SIGKILL

async def stop(self) -> None:
    pid_path = Path(f"{self.workdir.name}/{self.idfile}")
    pid: int | None = None
    if pid_path.exists():
        with contextlib.suppress(ValueError):
            pid = int(pid_path.read_text().strip())
    if pid is not None:
        self.peak_rss_kb = _read_vm_hwm(pid)

    try:
        await asyncio.shield(self._qmp_shutdown())
    finally:
        await super().stop()  # waits for `timeout` wrapper, kills after 5s

async def _qmp_shutdown(self) -> None:
    if self.qmp is None:
        return
    try:
        async with self.qmp.listener(("SHUTDOWN",)) as shutdown:
            try:
                await self.qmp.execute("system_powerdown")
            except qemu.qmp.QMPError:
                pass  # link wedged; jump straight to quit
            try:
                async with asyncio.timeout(GRACEFUL_SHUTDOWN_SECONDS):
                    await shutdown.get()
            except TimeoutError:
                with contextlib.suppress(qemu.qmp.QMPError):
                    await self.qmp.execute("quit")
    except (qemu.qmp.QMPError, EOFError, OSError):
        # Connection dropped mid-dance; super().stop() will SIGKILL.
        pass
    finally:
        with contextlib.suppress(Exception):
            await self.qmp.disconnect()
        self.qmp = None
```

`GRACEFUL_SHUTDOWN_SECONDS = 15` (vs today's 5 s SIGTERM grace). systemd shutdown jobs are slow; 5 s is too tight. 15 s sits comfortably inside `WRAPPER_GRACE_SECONDS = 60`.

## Error handling matrix

| Failure | Today | Proposed |
| --- | --- | --- |
| qemu binary fails execve | `proc.returncode` non-zero, ensure_booted raises RuntimeError. | Same — connect retry loop checks `proc.returncode` every iteration. |
| qemu prints early panic and exits | `proc.returncode` before pidfile appears. | `proc.returncode` before QMP socket appears, OR `ConnectError` if it died after listen(). |
| QMP socket never appears | n/a | `IDFILE_TIMEOUT` (60 s) deadline, raises `TimeoutError`. |
| `system_powerdown` ignored by guest (ZBM, initramfs without ACPI handler, cloud-init pre-systemd) | n/a — SIGTERM is the only path; qemu's ACPI translation is the same. | `asyncio.timeout(GRACEFUL_SHUTDOWN_SECONDS)` fires, fall through to `quit`. |
| QMP connection drops mid-test (qemu segfault) | proc dies, next ssh command fails, role test fails normally. | Same. Listener notices EOF; `_qmp_shutdown` swallows the disconnection; super().stop() waits for the host process. |
| User Ctrl-C | `cancel_on_signal` cancels task; `__aexit__` runs stop(); `asyncio.shield(terminate_pid(...))` survives a second Ctrl-C. | Same shape: `asyncio.shield(self._qmp_shutdown())`. Worst-case latency: 15 s graceful + 5 s wrapper-drain. |
| `quit` itself fails (qmp wedged) | n/a | `_qmp_shutdown` returns; super().stop() waits 5 s then `proc.kill()` — equivalent to today. |
| `--keep` interactive session | SIGTERM on Ctrl-C, qemu exits via ACPI. | `system_powerdown` + `quit` fallback. The user's separate `--qmp` socket (see below) is unaffected. |

## launch.py interaction

launch.py's `--qmp SOCKET` adds a second `-qmp unix:SOCKET,server,nowait` chardev. With the harness now adding its own, qemu happily supports multiple QMP listeners — they're independent chardevs.

**Recommendation: keep them separate.**

- Harness's QMP socket lives at `{workdir}/qmp.sock`, owned and managed internally, never advertised to the user. Vanishes when the workdir is cleaned up.
- launch.py's `--qmp PATH` keeps doing what it does today.

Two reasons not to share:

1. The harness pre-loads listeners and may have in-flight commands. Sharing means contending with whatever the user's interactive client is doing — including potential `quit` commands racing ours.
2. QMP doesn't multiplex commands across clients well; command-id collisions are possible. Two sockets = isolation.

Marginal cost: one extra file in the workdir, one extra `-qmp` flag. Acceptable.

Special case: launch.py's `--foreground` path uses `subprocess.Popen` and skips `ensure_booted`/`stop` entirely; it also strips the `timeout` wrapper. The harness's QMP socket would still be added by `_boot_command`, but no one connects to it; the socket file is cleaned up with the workdir in the `finally`. No behaviour change there.

## `timeout` wrapper

Two reasons it exists:

1. **Last-resort kernel-level kill** if Python's deadline overshoots and `Machine.stop()` is never reached. With QMP-driven `quit` the relevant Python path is more reliable, but the wrapper still covers things QMP cannot — e.g. qemu hung in a way that ignores both QMP and SIGTERM (rare; bug reports exist for `-accel hvf` deadlocks on Apple Silicon).
2. **`--keep --timeout 0` semantics** — `wrapper_timeout = 0` runs forever (interactive mode wants this).

QMP doesn't replace either. **Recommendation: keep the wrapper unchanged.** Inner Python timeouts (`asyncio.timeout(GRACEFUL_SHUTDOWN_SECONDS)`, then super().stop()'s 5 s) fire well before the wrapper does; the wrapper only matters when *all* of them fail.

Future follow-up (out of scope here): drop `WRAPPER_GRACE_SECONDS` from 60 → ~30 once QMP is the primary shutdown path. New worst-case is ~21 s (15 s graceful + sub-second `quit` + 5 s `proc.wait`); 30 s gives 9 s of slop.

## Pidfile

No `query-pid` QMP command exists, so `-pidfile` stays. It's:

- The only way to learn qemu's host PID for `_read_vm_hwm`.
- Free (one file per VM).
- Already produced today.

Reading it is no longer in the boot-detection path (QMP is). It's read once in `stop()` to feed `_read_vm_hwm(pid)`, then dropped on the floor. Mac continues to return 0 from `_read_vm_hwm` because there's no `/proc` — unchanged, documented limitation.

## Risks

1. **ACPI shutdown reliability on direct-kernel-boot variants.** aarch64 ZFS path direct-boots a kernel with `console=ttyAMA0,115200` and a ZFS rootfs; that kernel does have ACPI on `virt`, and Ubuntu's systemd handles power-button events, so `system_powerdown` should work. **But** during the first ~5–10 s — initramfs phase, before systemd takes over — there's no ACPI handler registered and `system_powerdown` is silently dropped. The 15 s grace covers this in normal "test ran, then shut down" flows where the guest is well into systemd; tighter if a test fails *in* initramfs and we shut down a half-booted guest. Fallback to `quit` covers it; cost is a guest-uncooperative shutdown, fine for a disposable test VM.

2. **ZBM / launch.py iteration mode.** launch.py sometimes boots a kernel that *isn't* Linux at all (zfsbootmenu binary) — no ACPI handler. `system_powerdown` will time out 100% of the time, fall through to `quit`. Behaviour is fine but slow (15 s of pointless waiting per teardown). Mitigation: launch.py's `--foreground` path bypasses harness stop logic entirely (subprocess.Popen, no QMP), so this only affects non-foreground launches. Acceptable.

3. **Minimal cloud-init variant.** `qemu_test_minimal=True` boots Ubuntu cloud images without our packer customisation. cloud-init takes a while during first boot. If a test fails very early (ssh banner timeout), `system_powerdown` may hit a guest pre-systemd. Same fallback applies.

4. **qemu.qmp dependency footprint.** One PyPI dep, pure Python, zero transitive deps, dual-licensed GPL/LGPL. Pin `qemu.qmp>=0.0.6` (older releases had packaging quirks).

5. **Mac hvf edge case.** A handful of issues report qemu wedging on `-accel hvf` such that even SIGTERM goes unanswered. In those cases QMP is also unreachable; only SIGKILL helps. This is exactly what the `timeout` wrapper exists for. No regression.

6. **Capability negotiation latency.** `connect()` adds a round-trip (greeting + `qmp_capabilities`). ~10 ms on a unix socket. Negligible.

7. **Socket-appearance race.** qemu creates the unix socket file slightly *after* the main loop becomes responsive. There's a small window where the file exists but `accept()` hasn't been called — surfaces as `ConnectionRefusedError` rather than `FileNotFoundError`. Retry loop handles both; keep both error types in the except clause and don't consolidate.

## Estimated LOC delta

Removed (in `test/machine.py`):

- `terminate_pid` import + call site in `QemuMachine.stop`: ~3 lines.
- (Optionally) base `Machine.ensure_booted` body: ~10 lines if we delete the pidfile poll outright. Safer to leave for now since `Machine` is the abstract base.

Added (in `test/machine.py`):

- `QMPClient` / error-type imports: 1 line.
- `qmp` / `qmp_socket_path` attributes on `QemuMachine`: ~3 lines.
- `-qmp` flag in `_boot_command`: 1 line.
- `QemuMachine.ensure_booted` override (connect retry loop): ~20 lines.
- `_qmp_shutdown` helper: ~25 lines.
- `GRACEFUL_SHUTDOWN_SECONDS` constant: 1 line.

Net: roughly **+50 / −20 = +30 LOC**. Replacement boot-detection body is comparable to the loop it replaces; the volume is in `_qmp_shutdown`. The big win is qualitative — three race-prone polling loops collapse to one event-driven shutdown with a structured exception model.

## Open questions / follow-ups (out of scope for this migration)

- Should the harness assert `query-status` returns `"running"` before `ensure_ssh()` runs? Probably not worth the latency; SSH banner check is the real readiness gate.
- Could `WRAPPER_GRACE_SECONDS` be lowered from 60 s to 30 s once QMP is primary? Defer until QMP has been in CI for a few weeks.
- Listen for `RESET` / `STOP` / `RESUME` events for diagnostics (e.g. detect a guest that triple-faulted during a role play)? Easy to bolt on once `qmp` is plumbed in. Not in this migration.
