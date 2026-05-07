# Output capture / colorization simplification plan

## Summary

The harness has a small bespoke output-capture stack:

- A module-global `_OUTPUT_LOG` `TextIO` plus `tee_output()` context manager in `test/utils.py` mirrors every `print_line` / `print_cmd_line` / subprocess output line into `test/out/<prefix>.output.ansi`.
- `read_and_write_stream()` decodes a process pipe line-by-line, optionally wrapping it in an ANSI red template before writing it through the same `_emit()` path.
- `colorize()` + a two-entry `COLORS` dict provide the ANSI templates.
- `testrole.py` emits a `PEAK_KB=<int>` sentinel line at end-of-run on stdout, which `testall.py` scrapes with a dedicated `_capture_peak_kb()` task.
- `Machine` writes three `.ansi`-suffixed artifacts: `output_file` (harness transcript, ANSI from the bespoke layer), `boot_file` (qemu serial stdio, plain text but mis-named `.ansi`), `journal_file` (`journalctl --no-pager` with `SYSTEMD_COLORS=true` over ssh, genuinely ANSI).

The plumbing is ~150 LOC of utils.py plus ad-hoc wiring. Pain points:

1. ANSI escape codes baked into the on-disk transcript -- grepping requires a `sed` strip; `bat`/`less -R` would prefer plain-text on disk plus a renderer.
2. `PEAK_KB=` travels through the same byte stream as ansible/qemu output -- one channel, two contracts.
3. Module-global `_OUTPUT_LOG` couples every helper to a hidden tee target.
4. `read_and_write_stream` is ~25 LOC of bespoke async pipe-drain + colorize + capture; trivial once colorization moves out.

The user's hint -- *stop colorizing the on-disk artifact and let the terminal renderer handle it* -- is the lever: plain text on disk, ANSI on stdout when stdout is a tty. Standard pattern; deletes most of the bespoke layer.

Plan: two-phase migration.

1. **Phase 1 (cheap, isolated):** replace `PEAK_KB=` stdout sentinel with a sidecar file `test/out/<prefix>.peak_kb`. Independent of color refactor; lets `_capture_peak_kb` and the dual-purpose stdout disappear.
2. **Phase 2 (the real win):** replace `tee_output` / `read_and_write_stream` / `colorize` / `print_line` / `print_cmd_line` with a ~30-LOC `Logger` class that writes plain text to a file and ANSI to stdout (only when tty). Drops ~120 LOC from utils.py and removes the global.

Phase 2 changes the on-disk format: `.output.ansi`/`.boot.ansi`/`.journal.ansi` → `.output.log`/`.boot.log`/`.journal.log`. The journal log still contains ANSI because journalctl wrote it directly; document and view with `bat` / `less -R`.

## Current data flows

```
                           ┌─ stdout (terminal)        ── ANSI ──
print_line ─────► _write_line ─► _emit ──┤
print_cmd_line ───► _write_line ─► _emit ──┤
                                           └─ _OUTPUT_LOG (tee_output handle) ── ANSI ──
                                              └─ test/out/<prefix>.output.ansi

run_command ─► subprocess (PIPE/PIPE)
              └─ read_and_write_stream(stdout, color=None) ─► _write_line ─► _emit ── (dual)
              └─ read_and_write_stream(stderr, color="red") ─► _write_line ─► _emit ── (dual)

QemuMachine.boot ─► subprocess.create(stdout=boot_file_handle, stderr=STDOUT, start_new_session=True)
                    └─ python NOT in path; qemu writes its own console straight to FD ──► <prefix>.boot.ansi
                       (file is plain text — qemu serial stdio doesn't ANSI-color)

Machine.collect_journal ─► ssh "env SYSTEMD_COLORS=true journalctl..." (PIPE for stderr only)
                             └─ stdout ──────► <prefix>.journal.ansi  (genuinely ANSI)
                             └─ stderr ──────► read_and_write_stream(red) ─► _emit ── (dual)

testrole.py end-of-run ─► print_line(f"PEAK_KB={n}") ─► stdout
                                                       └─ testall._capture_peak_kb scrapes stdout pipe
```

| Artifact | Writer | ANSI on disk? | Notes |
|---|---|---|---|
| stdout (terminal) | `_emit` via `print_line` etc. | Yes (when wanted) | Live tail; user expects color on tty. |
| `<prefix>.output.ansi` | `_emit` via tee global | Yes | Transcript. Currently colorized. |
| `<prefix>.boot.ansi` | qemu, direct kernel write | No | Already plain. Misnamed. |
| `<prefix>.journal.ansi` | journalctl over ssh, direct write | Yes | Python not in path; ANSI is upstream's. |
| `PEAK_KB=` line | print_line ─► stdout | n/a | Side-channel piggybacking on transcript. |

Two observations matter for the redesign:

- For `output_file`, Python *is* in the path. We have full control.
- For `boot_file` and `journal_file`, Python is *not* in the path -- the child writes straight to a dup'd FD. We can't strip ANSI without re-introducing a Python pipe drainer (which is exactly the layer the current design avoids by using direct kernel writes; that direct-write decision is good and shouldn't change). So "no ANSI on disk" must make a per-artifact exception for `journal_file`: it stays ANSI because journalctl wrote it that way.

## Simplification options

### Option A — `rich.Console` with two destinations

Drop the bespoke layer for a `rich.Console(file=tty)` plus a second `rich.Console(file=path, no_color=True)` writer.

- **Pros:** mature, scales beyond two colors, built-in `export_text()`.
- **Cons:** **`rich` is not in `uv.lock`** (verified). Adds ~10 transitive deps and ~50ms cold import per testrole.py invocation × N parallel jobs. Not justified by current usage (exactly two ANSI styles).

### Option B — stdlib `logging` with two handlers

`StreamHandler` (color via custom formatter, fed from `sys.stdout.isatty()`) + `FileHandler` (plain).

- **Pros:** stdlib only, idiomatic, thread/process-safe handler dispatch.
- **Cons:** ceremony (root logger, propagation, names). Ansible/qemu output isn't log records, it's lines; forcing through `logging.info()` works but is overkill.

### Option C — minimal custom `Logger` class **(RECOMMENDED)**

A small dataclass that owns one open file handle and the "is tty?" decision. ~30 LOC including docstrings. Replaces `tee_output`, `_OUTPUT_LOG`, `_emit`, `_write_line`, `print_line`, `print_cmd_line`, `colorize`, `COLORS`, and `read_and_write_stream`'s output side.

- **Pros:** zero deps. ~120 LOC saved. No global state. Easy to test (StringIO + force_color).
- **Cons:** every call site must pass the logger explicitly (or carry it on `Machine`). Mitigated by making it a `Machine` attribute.

### Option D — keep ANSI on disk, swap to `script(1)` / `unbuffer`

- **Pros:** no Python work.
- **Cons:** doesn't address the on-disk ANSI complaint, adds an external-tool dep (BSD `script` on macOS has different flags than GNU `script`), doesn't help `PEAK_KB`.

### Recommendation: **Option C**.

Rationale:

- The two colors we use ("cyan command echo", "red stderr/error") are trivial without a library.
- `tee_output`'s module-global is the architectural smell, not the colorize helper -- replacing it with a passed-around `Logger` is the change that meaningfully reduces coupling.
- testall.py's lone `colorize("fail", "red")` is independent of the per-run transcript and can use a 3-line inline helper.
- Plain-text on-disk falls out for free.

## Should `boot_file` / `output_file` / `journal_file` stay separate?

**Yes.** Three different writers (harness Python; qemu via direct FD dup; journalctl over ssh via direct FD dup), three lifetimes (per-boot truncate; failure-only; per-run open), three formats (kernel/systemd console; journalctl; harness narrative), three consumers (`print_file_tail` on infra failure; consulted on role failure; canonical narrative). Merging would require re-introducing Python pipe drainers for boot+journal -- the layer we deliberately avoid by passing the file FD into `create_subprocess_exec(stdout=handle)`.

Suffix policy: rename all three to `.log` for naming consistency. The journal log still contains ANSI; document that and view with `bat` / `less -R` (or `sed` to strip).

## PEAK_KB sentinel — replace with a sidecar file

Three options:

- **A. Sidecar file in `test/out/`.** `Machine.stop()` writes `_read_vm_hwm(pid)` to `OUT_DIR / f"{prefix}.peak_kb"` (one int + newline). testall.py reads it after `proc.wait()`; testrole.py drops it under `--no-keep-logs`.
- **B. Structured JSON line on stdout.** Doesn't fix the dual-purpose-stdout problem. Just prettier.
- **C. testall.py imports testrole as a library.** Bigger refactor (argparse, signal handling, asyncio.run all need restructuring). Out of scope.

**Recommend A.** Concrete shape:

```python
# in machine.py, alongside the other artifact paths
self.peak_kb_file = OUT_DIR / f"{prefix}.peak_kb"

# in QemuMachine.stop() after self.peak_rss_kb is set
if self.peak_rss_kb > 0:
    self.peak_kb_file.write_text(f"{self.peak_rss_kb}\n")

# in testall.py _run_role(), after proc.wait()
peak_kb_path = OUT_DIR / f"{machine}.{ubuntu_name}.{role}.peak_kb"
peak_kb = 0
try:
    peak_kb = int(peak_kb_path.read_text().strip())
except (FileNotFoundError, ValueError):
    pass  # measurement unavailable (early failure, macOS host, etc.)
```

Net changes:
- `PEAK_KB_SENTINEL_PREFIX` deleted from machine.py.
- `_capture_peak_kb()` task and `peak_reader` plumbing deleted (~25 LOC).
- testall.py can drop `stdout=PIPE`. **Inheriting stdout is fine** for testall's children: each child's output is in its own `.log`; parallel interleave on the parent terminal is what the per-role file solves. The `[seq] role:machine starting` summary already prints separately.
- `Machine.cleanup_logs()` adds `.peak_kb` to its unlink list.
- `setup_output_dir()` adds `.peak_kb` to the pre-clean loop.

Safe to do alone; doesn't touch colorization.

## Concrete code sketches

### New `test/utils.py` (relevant excerpt, replaces ~150 LOC)

```python
import dataclasses
import shlex
import sys
from typing import TextIO

# Two ANSI styles. Inline rather than a dict because we use exactly these two.
_CYAN = "\033[0;36m"
_RED = "\033[0;41m"
_RESET = "\033[0m"


@dataclasses.dataclass
class Logger:
    """Per-run logger. Plain text to file, ANSI to stdout (only when tty)."""

    file: TextIO  # opened by caller, e.g. (OUT_DIR/"<prefix>.output.log").open("w")
    stream: TextIO = dataclasses.field(default_factory=lambda: sys.stdout)
    color: bool = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        self.color = self.stream.isatty()

    def line(self, text: str, *, error: bool = False) -> None:
        """Echo a free-form line. Red on stdout when error=True; plain on disk."""
        self.file.write(text + "\n")
        self.file.flush()
        if error and self.color:
            self.stream.write(f"{_RED}{text}{_RESET}\n")
        else:
            self.stream.write(text + "\n")
        self.stream.flush()

    def cmd(self, cmd: list[str], env: dict[str, str] | None = None) -> None:
        """Echo the command being executed. Cyan on stdout, plain on disk."""
        if env:
            env_parts = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
            text = f"$ env {env_parts} {shlex.join(cmd)}"
        else:
            text = f"$ {shlex.join(cmd)}"
        self.file.write(text + "\n")
        self.file.flush()
        if self.color:
            self.stream.write(f"{_CYAN}{text}{_RESET}\n")
        else:
            self.stream.write(text + "\n")
        self.stream.flush()
```

Shape decisions:

- Logger owns its file handle. Caller opens inside `with`; testrole.py threads it into `run_command` / `Machine`.
- No global state.
- `isatty()` checked once. testrole.py interactive → tty, color on. testall.py inherits stdout → still a tty → user sees colored interleaved output for parallel jobs even though each `.log` stays plain.
- Single style per method (no `color=` arg); commands cyan, errors red, everything else plain. Matches actual call sites.

### New `run_command` (relevant excerpt)

```python
async def run_command(
    cmd: list[str],
    log: Logger,
    *,
    check: bool = True,
    quiet: bool = False,
    env: dict[str, str] | None = None,
    cleanup_grace_seconds: float = 0.0,
    cleanup_signal: int = signal.SIGKILL,
) -> CommandResult:
    if not quiet:
        log.cmd(cmd, env=env)

    subprocess_env = {**os.environ, **env} if env is not None else None
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=subprocess_env,
    )
    assert process.stdout is not None and process.stderr is not None

    stdout: list[str] = []
    stderr: list[str] = []
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_drain(process.stdout, stdout, log, error=False, quiet=quiet))
            tg.create_task(_drain(process.stderr, stderr, log, error=True,  quiet=quiet))
        exitcode = await process.wait()
    except BaseException:
        await terminate_subprocess(process, grace_seconds=cleanup_grace_seconds, initial_signal=cleanup_signal)
        raise

    if check and exitcode != 0:
        raise CommandFailedException(cmd, exitcode, stderr)
    return CommandResult(exitcode=exitcode, stdout=stdout, stderr=stderr)


async def _drain(
    stream: asyncio.StreamReader,
    capture: list[str],
    log: Logger,
    *,
    error: bool,
    quiet: bool,
) -> None:
    """Read line-by-line, capture each line, optionally log it."""
    while True:
        line_bytes = await stream.readline()
        if not line_bytes:
            return
        line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
        capture.append(line)
        if not quiet:
            log.line(line, error=error)
```

`read_and_write_stream` → `_drain` (10 LOC, loses the `color` parameter -- error=True does the colorize implicitly). Same TaskGroup pattern; existing comment about cross-stream interleave still applies.

### Wire-up in testrole.py

```python
m: Machine = QemuMachine(...)
with m.output_file.open("w") as logfile:
    log = Logger(file=logfile)
    m.log = log
    rc = 0
    try:
        asyncio.run(run_test(m, log, pass_args, ...))
    except CommandFailedException as exc:
        log.line(str(exc), error=True)
        log.line(f"{role}.{machine} failed", error=True)
        rc = 1
    # ... etc
    finally:
        # peak_kb sidecar already written by Machine.stop(); no sentinel print.
        _print_phase_summary(log)
```

### Wire-up in machine.py

```python
@dataclasses.dataclass
class Machine:
    # ... existing fields ...
    log: Logger = dataclasses.field(init=False)  # set by caller after __init__

    async def ssh_command(self, *cmd: str, check: bool = True) -> CommandResult:
        return await run_command(self.format_ssh_cmd(*cmd), self.log, check=check)

    def print_file_tail(self, path: Path, n: int = 50) -> None:
        if not path.exists():
            return
        with path.open("r", errors="replace") as handle:
            tail = list(collections.deque(handle, maxlen=n))
        tail = [line.rstrip("\n") for line in tail]
        self.log.line(f"--- last {len(tail)} lines of {path} ---")
        for line in tail:
            self.log.line(line)
        self.log.line(f"--- end {path} ---")
```

Every `print_line(...)` in machine.py → `self.log.line(...)`; every `print_cmd_line(cmd)` → `self.log.cmd(cmd)`. The `read_and_write_stream` inside `collect_journal` → `_drain(proc.stderr, [], self.log, error=True, quiet=False)`.

### Dropped from utils.py

| Symbol | Replacement | LOC saved |
|---|---|---|
| `_OUTPUT_LOG` | gone (no global) | 1 |
| `tee_output()` | `Path.open("w")` + `Logger(...)` | 13 |
| `_emit()` | inlined into `Logger.line/cmd` | 8 |
| `_write_line()` | inlined into `Logger.line/cmd` | 4 |
| `colorize()` | inlined `_RED`/`_CYAN` constants | 4 |
| `COLORS` dict | inlined constants | 4 |
| `print_cmd_line()` | `Logger.cmd` | 13 |
| `print_line()` | `Logger.line` | 9 |
| `read_and_write_stream()` | `_drain()` | ~10 |

testall.py keeps a tiny inline helper for `colorize("fail", "red")`:

```python
_RED, _RESET = "\033[0;41m", "\033[0m"
status = "ok" if exitval == 0 else (f"{_RED}fail{_RESET}" if sys.stdout.isatty() else "fail")
```

## Migration phases

### Phase 1 — PEAK_KB sidecar (small, isolated)

1. `machine.py`: add `peak_kb_file = OUT_DIR / f"{prefix}.peak_kb"` field; have `QemuMachine.stop()` write `peak_rss_kb` to it after `_read_vm_hwm()`. Add to `cleanup_logs()`.
2. `testall.py`: delete `_capture_peak_kb`, delete `PEAK_KB_SENTINEL_PREFIX` import. Replace `peak_reader` task with a post-`proc.wait()` read of the sidecar. Drop `stdout=PIPE`. Add `.peak_kb` to `setup_output_dir`'s pre-clean loop.
3. `testrole.py`: delete the `print_line(f"PEAK_KB=...")` line in `main()`'s `finally`. Delete the import.
4. `machine.py`: delete the `PEAK_KB_SENTINEL_PREFIX` constant.

Verification: `test/testrole.py <role>` produces `test/out/<prefix>.peak_kb`; `test/testall.py --machines box --retry-role <one role>` populates `PeakKB` in `test/out.tsv`.

LOC: -25 testall, -3 testrole, -2 machine constant, +6 machine sidecar = **-24 net**.

### Phase 2 — Logger refactor (substantive)

Order matters; one PR but commit in this order for bisectability:

1. **Add `Logger` class to utils.py.** Don't delete old helpers yet; both APIs coexist.
2. **Convert `run_command` to take `log: Logger`.** Update all callers (`machine.py` ssh/ansible/scp/qemu-img, `testrole.py` via `m.ansible_command`, `launch.py`). Pass a temporary "shim" logger that delegates to old `print_line`/`print_cmd_line`. Mechanical, large, no behavior change.
3. **Switch testrole.py and launch.py from `tee_output` to `with output_file.open("w") as f: log = Logger(file=f)`.** Shim goes away. Behavior changes: `.output.ansi` is now plain.
4. **Rename suffixes** (`.output.ansi` → `.output.log`, `.boot.ansi` → `.boot.log`, `.journal.ansi` → `.journal.log`). Update `setup_output_dir`. Update `AGENTS.md` line 43. Note in commit message that journal log still contains ANSI (journalctl wrote it).
5. **Delete unused old helpers from utils.py:** `tee_output`, `_OUTPUT_LOG`, `_emit`, `_write_line`, `print_line`, `print_cmd_line`, `colorize`, `COLORS`, `read_and_write_stream`. Replace `testall.py`'s `colorize("fail", "red")` with the 3-line inline helper.
6. **Make `m.log` an explicit constructor argument.** Cleans up the "constructed without a logger then assigned" awkwardness.

Verification at each step:

- After step 2: unit tests pass; `<prefix>.output.ansi` unchanged.
- After step 3: `cat <prefix>.output.ansi` is plain; `test/testrole.py <role>` is colored in terminal.
- After step 5: `wc -l test/utils.py` is ~205 (down from 325); `python -c "from utils import print_line"` raises ImportError.

Per `feedback_run_before_commit.md`: run `test/testrole.py systemd_unit --machine minimal --no-checkmode --no-idempotence` (cheapest happy path) end-to-end before each commit.

## Estimated LOC delta

| File | Before | After | Delta |
|---|---|---|---|
| test/utils.py | 325 | ~205 | -120 |
| test/machine.py | 1051 | ~1040 | -11 |
| test/testrole.py | 378 | ~365 | -13 |
| test/testall.py | 564 | ~530 | -34 |
| test/launch.py | (uses 5 helpers) | (uses logger) | ~0 |
| **total** | **2318** | **~2140** | **-178** |

Phase 1 alone: -24. Phase 2 alone: -154.

Subjective improvements not visible in LOC:

- No module-global state in utils.py.
- One contract per channel: stdout is the live view, files are artifacts, `.peak_kb` sidecar is the structured measurement.
- On-disk artifacts grep-friendly (no embedded ANSI in `.output.log` / `.boot.log`; only `.journal.log` retains ANSI because journalctl wrote it directly).
- `Logger` is testable in isolation (StringIO).

## Open questions / non-decisions

- **`colorama` for cross-platform ANSI on Windows:** skip. Mac arm64 first-class; ANSI works natively in macOS Terminal / iTerm2.
- **`--no-color` CLI flag:** the `isatty()` check covers piping/redirect. Add the flag if/when a CI consumer asks; out of scope.
- **`boot_file` ANSI stripping:** already plain (qemu serial stdio uncolored), no action.
