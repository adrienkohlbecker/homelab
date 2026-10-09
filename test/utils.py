import argparse
import atexit
import contextlib
import itertools
import os
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import zlib
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import IO, NamedTuple, TextIO


class CommandFailedException(Exception):
    """Raised when a subprocess exits with a non-zero status."""

    def __init__(self, cmd: list[str], exitcode: int, stderr: list[str], stdout: Sequence[str] = ()) -> None:
        self.cmd = cmd
        self.exitcode = exitcode
        self.stderr = stderr
        self.stdout = list(stdout)
        tail = "\n".join(stderr[-20:])
        suffix = f"\n--- stderr tail ---\n{tail}" if tail else ""
        super().__init__(f"Command failed with exit code {exitcode}: {shlex.join(cmd)}{suffix}")


class IdempotenceFailedException(Exception):
    """Raised when re-running an ansible play reports changed tasks."""


class CheckFailedException(Exception):
    """Raised when a harness check on the converged guest fails."""


class CommandResult(NamedTuple):
    """Outcome of a subprocess invocation."""

    exitcode: int
    stdout: list[str]


# SGR parameters for colorize().
COLORS = {"red": "0;41", "cyan": "0;36", "green": "0;32"}

# Compact console mode, set by testrole.py unless --verbose: only status lines
# (print_line) reach the terminal, each prefixed with this cell's tag, so cells
# interleaving under GNU parallel stay readable. Subprocess output and the
# commands themselves go to the run log alone.
_CONSOLE_TAG: str | None = None
# Distinct 256-colour foregrounds; a role always gets the same one.
_TAG_COLORS = (33, 39, 70, 75, 99, 135, 166, 172, 178, 204, 37, 141)
# How often a long phase reports it is still running in compact mode.
PHASE_HEARTBEAT_SECONDS = 300
# Whether the compact console is a terminal, the only place cursor movement
# means anything: GNU parallel and CI job logs read a pipe.
_REWRITABLE = False
# The token of the phase whose header is the terminal's last line, so its
# heartbeat and result may overwrite it rather than append (see _emit).
_HELD_LINE: object | None = None
# Cursor up one line, back to its first column, and clear it.
_REWRITE_PREVIOUS_LINE = "\033[1A\r\033[2K"

# The run's transcript, set by tee_output(): everything _emit writes, whatever
# the console shows.
_OUTPUT_LOG: TextIO | None = None


@contextlib.contextmanager
def tee_output(path: Path) -> Iterator[None]:
    """Write everything _emit sends into *path* for the duration of the with-block."""
    global _OUTPUT_LOG
    with path.open("w") as handle:
        _OUTPUT_LOG = handle
        try:
            yield
        finally:
            _OUTPUT_LOG = None


def positive_seconds(value: str) -> int:
    """argparse type for a --timeout: 0 would disarm the session deadline."""
    seconds = int(value)
    if seconds < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1 second, got {seconds}")
    return seconds


def use_compact_console(role: str, cell: str) -> None:
    """Show only status lines on the terminal, tagged ``role cell`` in a colour
    derived from the role."""
    global _CONSOLE_TAG, _REWRITABLE
    _REWRITABLE = sys.stdout.isatty()
    color = _TAG_COLORS[zlib.crc32(role.encode()) % len(_TAG_COLORS)]
    _CONSOLE_TAG = f"\033[38;5;{color}m{f'{role} {cell}':<28}\033[0m"


def _elapsed(start: float) -> str:
    minutes, seconds = divmod(int(time.monotonic() - start), 60)
    return f"{minutes}:{seconds:02d}"


@contextlib.contextmanager
def phase(name: str) -> Iterator[None]:
    """Report a test phase's start, result, and duration as status lines.

    On a terminal, the heartbeat and the result overwrite the header line as
    long as nothing else has reached the console since; elsewhere each is a
    line of its own.
    """
    start = time.monotonic()
    token = object()
    _write_line(f"▶ {name}", None, status=True, hold=token)
    done = threading.Event()
    # Marking the phase done and printing a heartbeat are ordered, so no
    # heartbeat that already woke up can land after the result line.
    reporting = threading.Lock()

    def heartbeat() -> None:
        while not done.wait(PHASE_HEARTBEAT_SECONDS):
            with reporting:
                if not done.is_set():
                    _write_line(f"▶ {name} ({_elapsed(start)})", None, status=True, hold=token, replace=token)

    threading.Thread(target=heartbeat, name=f"phase-{name}", daemon=True).start()
    passed = False
    try:
        yield
        passed = True
    finally:
        with reporting:
            done.set()
        mark, color = ("✓", "green") if passed else ("✗", "red")
        _write_line(f"{mark} {name} ({_elapsed(start)})", color, status=True, replace=token)


ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TASK_HEADER_RE = re.compile(r"(TASK|RUNNING HANDLER) \[")
_TASK_RESULT_RE = re.compile(r"(ok|changed|skipping|included|failed|fatal): ")
_TASK_FAILURE_RE = re.compile(r"(failed|fatal): \[")


def failed_tasks(stdout: Sequence[str]) -> list[list[str]]:
    """Return the block of ansible-playbook output for each task that failed,
    in order, or an empty list when none did.

    A block runs from the task's header through its last failure result,
    leaving out its passing and skipped loop items. Every failure counts,
    rescued ones too, except a task's that `...ignoring` follows. Matching
    ignores colour, but the lines come back as printed.
    """
    plain = [ANSI_CSI_RE.sub("", line) for line in stdout]
    # Ansible indents a YAML result's body under its unindented first line, so
    # each unindented line starts a segment that runs to the next one.
    starts = [i for i, line in enumerate(plain) if line and not line[0].isspace()]
    blocks: list[list[int]] = []
    task: list[int] = []
    # How much of the task's block runs through its last failure; 0 for none.
    failed = 0

    def close_task() -> None:
        block = task[:failed]
        while block and not plain[block[-1]].strip():
            block.pop()
        if block:
            blocks.append(block)

    for start, end in itertools.pairwise([*starts, len(plain)]):
        head = plain[start]
        if _TASK_HEADER_RE.match(head):
            close_task()
            task, failed = list(range(start, end)), 0
        elif _TASK_FAILURE_RE.match(head):
            task.extend(range(start, end))
            failed = len(task)
        elif head.startswith("...ignoring"):
            failed = 0
        elif not _TASK_RESULT_RE.match(head):
            task.extend(range(start, end))
    close_task()
    return [[stdout[i] for i in block] for block in blocks]


def task_title(header: str) -> str:
    """A TASK header line without its colour and trailing row of stars."""
    return re.sub(r" \*+$", "", ANSI_CSI_RE.sub("", header).rstrip())


def report_failure(excerpt: Sequence[str], log: Path, failure_file: Path, lines: int = 40) -> None:
    """Show a failed run's evidence and where its transcript lives, and keep
    the evidence in *failure_file* for test:all's closing summary.

    The evidence is *excerpt* when there is one, else the transcript's last
    *lines* lines. Only compact mode prints it, since verbose mode streamed it
    already, and only to the console, since the transcript holds it too.
    """
    if not excerpt:
        with contextlib.suppress(OSError):  # an unreadable log still leaves its path below
            excerpt = log.read_text(errors="replace").splitlines()[-lines:]
    failure_file.write_text("".join(f"{line}\n" for line in excerpt))
    if _CONSOLE_TAG is not None:
        _console("".join(f"{_CONSOLE_TAG} │ {line}\n" for line in excerpt) + f"{_CONSOLE_TAG} log: {log}\n")


_LAST_TICK = 0.0


def sleep_tick(seconds: float = 1.0) -> None:
    """Sleep *seconds*, emitting at most one progress dot per second.

    Sub-second polls keep the transcript's once-a-second heartbeat.
    """
    global _LAST_TICK
    now = time.monotonic()
    if now - _LAST_TICK >= 1:
        _emit(".")
        _LAST_TICK = now
    time.sleep(seconds)


def colorize(line: str, color: str | None) -> str:
    """Return *line* wrapped in the ANSI codes for the COLORS key *color*, or
    as is when *color* is None."""
    return f"\033[{COLORS[color]}m{line}\033[0m" if color else line


# Subprocess output is relayed line-by-line by the thread draining the child's
# pipe (run_command). A direct sys.stdout.write+flush there makes every line a
# blocking write(2): if whoever drains our stdout (a CI job-log pipe) stalls,
# the relay stops draining, the child blocks on its full pipe, and the cell
# hangs until its deadline. Hand the stdout half to a dedicated daemon thread
# so the relay only ever enqueues; the tee-file write stays inline (local disk,
# the authoritative transcript) so test/out/*.ansi stays complete even if
# stdout wedges and the daemon is killed at interpreter exit.
_STDOUT_QUEUE: queue.SimpleQueue[str | threading.Event] = queue.SimpleQueue()
# Serializes _emit across the stderr relay and phase heartbeat threads, so
# their lines never splice into one another in the transcript.
_EMIT_LOCK = threading.Lock()


def _stdout_writer_loop() -> None:
    while True:
        item = _STDOUT_QUEUE.get()
        if isinstance(item, threading.Event):
            # Drain barrier (see _drain_stdout): everything queued ahead of it
            # has been written, so release the waiter.
            item.set()
            continue
        try:
            sys.stdout.write(item)
            sys.stdout.flush()
        except BrokenPipeError, ValueError:
            # Consumer closed the pipe, or stdout was closed during shutdown:
            # nothing to write to. Keep draining so producers never block.
            pass


threading.Thread(target=_stdout_writer_loop, name="harness-stdout-writer", daemon=True).start()


@atexit.register
def _drain_stdout(timeout: float = 2.0) -> None:
    """Flush queued stdout on a clean exit, bounded so a wedged pipe can't hang.

    The writer is a daemon thread, so the interpreter won't wait on it; push a
    barrier and wait briefly for the queue ahead of it to drain so a normal run
    keeps its tail output. A stuck stdout just times out here and the daemon
    dies with the interpreter.
    """
    barrier = threading.Event()
    _STDOUT_QUEUE.put(barrier)
    barrier.wait(timeout)


def _console(text: str) -> None:
    """Queue console-only *text*; it ends any held line's claim to rewriting."""
    global _HELD_LINE
    with _EMIT_LOCK:
        _HELD_LINE = None
        _STDOUT_QUEUE.put(text)


def _emit(text: str, *, status: bool = False, hold: object | None = None, replace: object | None = None) -> None:
    """Queue *text* for stdout and write it to the transcript, if any.

    In compact console mode only *status* text reaches stdout, one tagged line
    at a time; everything still lands in the transcript. On a terminal, *hold*
    marks the console line as rewritable under that token, and *replace*
    overwrites the held line when its token matches, which holds only while no
    other console output has followed it. The transcript always gets plain
    appended lines.
    """
    global _HELD_LINE
    with _EMIT_LOCK:
        if _CONSOLE_TAG is None:
            console = text
        elif status:
            console = "".join(f"{_CONSOLE_TAG} {line}\n" for line in text.splitlines())
        else:
            console = ""
        if console:
            if replace is not None and replace is _HELD_LINE:
                console = _REWRITE_PREVIOUS_LINE + console
            _HELD_LINE = hold if _REWRITABLE else None
            _STDOUT_QUEUE.put(console)
        if _OUTPUT_LOG is not None:
            _OUTPUT_LOG.write(text)
            _OUTPUT_LOG.flush()


def _write_line(
    line: str, color: str | None, *, status: bool = False, hold: object | None = None, replace: object | None = None
) -> None:
    """Emit a line, optionally colorized; *status*, *hold*, and *replace* as
    for _emit."""
    _emit(colorize(line, color) + "\n", status=status, hold=hold, replace=replace)


def print_cmd_line(cmd: list[str], env: dict[str, str] | None = None) -> None:
    """Log the command being executed in a distinct color.

    When *env* is supplied, render an `env K=V K=V ... cmd ...` prefix so the
    printed line stays copy-pasteable.
    """
    if env:
        env_parts = [f"{k}={shlex.quote(v)}" for k, v in env.items()]
        _write_line(f"$ env {' '.join(env_parts)} {shlex.join(cmd)}", "cyan")
    else:
        _write_line(f"$ {shlex.join(cmd)}", "cyan")


def print_line(line: str, error: bool = False) -> None:
    """Write a status line: the console shows it in every mode, and the
    transcript keeps it. Pass error=True for the red highlight used for
    subprocess stderr.
    """
    _write_line(line, "red" if error else None, status=True)


def log_line(line: str, error: bool = False) -> None:
    """Write a line to the transcript; the console shows it only in verbose mode."""
    _write_line(line, "red" if error else None)


def _relay(stream: IO[str], color: str | None, capture: list[str]) -> None:
    """Relay a process stream to the transcript, and to the console in
    verbose mode, capturing each line."""
    for line in stream:
        line = line.rstrip("\r\n")
        capture.append(line)
        _write_line(line, color)


# How long an interrupted run_command waits for its output relays. Killing the
# command's process group closes the pipes unless something outside the group
# still holds them; the relays are daemon threads, so abandoning them costs
# nothing.
RELAY_DRAIN_SECONDS = 5
# How long an interrupted command gets to exit on SIGINT before its process
# group is killed. ansible-playbook uses it to stop its worker processes,
# which setsid() out of the group: killing the group first would also kill
# the Mitogen mux they talk through and leave them hung.
COMMAND_STOP_GRACE_SECONDS = 10


# macOS answers EPERM rather than ESRCH for a process group left holding only
# zombies, as one is between a SIGKILL and the reap; either way nothing live
# remains to signal.
_GROUP_GONE = (ProcessLookupError, PermissionError)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except _GROUP_GONE:
        return False
    return True


def stop_process_group(proc: subprocess.Popen, *, grace_seconds: float) -> None:
    """SIGTERM *proc*'s process group, SIGKILL whatever outlives *grace_seconds*,
    and reap *proc*.

    *proc* must lead its own group (start_new_session=True). Judges by the
    group rather than the leader: a leader that exits first (a killed
    `timeout` wrapper, a shell that dies on SIGTERM) can leave members behind.
    """
    pgid = proc.pid
    # Reap an exited leader, which would otherwise keep the group alive as a
    # zombie and hide whether anything real is left in it.
    proc.poll()
    with contextlib.suppress(*_GROUP_GONE):  # the group already exited
        os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while _group_alive(pgid) and time.monotonic() < deadline:
        proc.poll()
        time.sleep(0.1)
    with contextlib.suppress(*_GROUP_GONE):  # everything exited within the grace
        os.killpg(pgid, signal.SIGKILL)
    proc.wait()


def _stop_command(process: subprocess.Popen) -> None:
    """Interrupt a run_command child, then kill whatever is left in its group."""
    with contextlib.suppress(ProcessLookupError):  # it already exited
        process.send_signal(signal.SIGINT)
    with contextlib.suppress(subprocess.TimeoutExpired):  # the group kill below ends it
        process.wait(COMMAND_STOP_GRACE_SECONDS)
    stop_process_group(process, grace_seconds=0)


def run_command(
    cmd: list[str], check: bool = True, *, env: dict[str, str] | None = None, timeout: float | None = None
) -> CommandResult:
    """Execute a subprocess, stream its output live and colorized.

    check raises CommandFailedException on a non-zero exit. env layers
    overrides on top of os.environ. timeout bounds the whole command,
    including draining its output; past it the command is stopped and
    TimeoutError raised. The command runs in its own process group. If it is
    interrupted (Ctrl-C, the timeout, a failed relay), it gets SIGINT and
    COMMAND_STOP_GRACE_SECONDS to stop, then the whole group is killed, so no
    descendant is left holding its pipes.
    """
    print_cmd_line(cmd, env=env)
    deadline = None if timeout is None else time.monotonic() + timeout

    def remaining() -> float | None:
        return None if deadline is None else max(0.0, deadline - time.monotonic())

    stdout: list[str] = []
    stderr: list[str] = []
    relay_failures: list[Exception] = []
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **env} if env is not None else None,
        text=True,
        errors="replace",
        start_new_session=True,
    )

    def relay(stream: IO[str] | None, color: str | None, capture: list[str]) -> None:
        assert stream is not None
        try:
            _relay(stream, color, capture)
        except Exception as exc:
            # Stopping the command closes its other pipe and ends the wait.
            relay_failures.append(exc)
            _stop_command(process)

    # Both streams drain on threads so neither pipe can fill and block the
    # command, and the main thread only waits, which Ctrl-C and the timeout
    # can always interrupt. Cross-stream order is therefore not preserved;
    # callers (ansible-playbook, ssh) emit nearly everything on stdout.
    relays = [
        threading.Thread(target=relay, args=(process.stdout, None, stdout), daemon=True),
        threading.Thread(target=relay, args=(process.stderr, "red", stderr), daemon=True),
    ]
    try:
        for thread in relays:
            thread.start()
        try:
            exitcode = process.wait(remaining())
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"{shlex.join(cmd)} did not finish within {timeout:.0f}s") from None
        # A descendant can keep the pipes open after the command exits.
        for thread in relays:
            thread.join(remaining())
        if any(thread.is_alive() for thread in relays):
            raise TimeoutError(f"{shlex.join(cmd)} did not finish within {timeout:.0f}s")
    except BaseException:
        _stop_command(process)
        for thread in relays:
            if thread.ident is not None:
                thread.join(RELAY_DRAIN_SECONDS)
        raise
    if relay_failures:
        raise relay_failures[0]

    if check and exitcode != 0:
        raise CommandFailedException(cmd, exitcode, stderr, stdout)
    return CommandResult(exitcode=exitcode, stdout=stdout)
