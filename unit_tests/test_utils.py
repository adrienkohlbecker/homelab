"""Unit tests for test/utils.py — tee_output, print, and process helpers."""

import contextlib
import os
import signal
import threading
import time
from pathlib import Path

import pytest
import utils

# ---------------------------------------------------------------------------
# tee_output
# ---------------------------------------------------------------------------


class TestTeeOutput:
    def test_writes_to_file(self, tmp_path: Path) -> None:
        log_path = tmp_path / "test.log"
        with utils.tee_output(log_path):
            utils._emit("hello\n")
        assert "hello" in log_path.read_text()


# ---------------------------------------------------------------------------
# print_cmd_line
# ---------------------------------------------------------------------------


class TestPrintCmdLine:
    def test_without_env(self, capsys: pytest.CaptureFixture) -> None:
        utils.print_cmd_line(["ls", "-la"])
        utils._drain_stdout()  # stdout is written on a background thread
        captured = capsys.readouterr()
        assert "ls -la" in captured.out

    def test_with_env(self, capsys: pytest.CaptureFixture) -> None:
        utils.print_cmd_line(["cmd"], env={"FOO": "bar"})
        utils._drain_stdout()
        captured = capsys.readouterr()
        assert "env FOO=bar cmd" in captured.out

    def test_quoting(self, capsys: pytest.CaptureFixture) -> None:
        utils.print_cmd_line(["echo", "hello world"])
        utils._drain_stdout()
        captured = capsys.readouterr()
        assert "'hello world'" in captured.out


# ---------------------------------------------------------------------------
# CommandFailedException
# ---------------------------------------------------------------------------


class TestCommandFailedException:
    def test_message_includes_cmd_and_exitcode(self) -> None:
        exc = utils.CommandFailedException(["git", "push"], 128, ["fatal: error"])
        assert "128" in str(exc)
        assert "git push" in str(exc)
        assert "fatal: error" in str(exc)

    def test_empty_stderr_has_no_tail(self) -> None:
        exc = utils.CommandFailedException(["ls"], 1, [])
        assert "stderr tail" not in str(exc)


# ---------------------------------------------------------------------------
# run_command
# ---------------------------------------------------------------------------


class TestRunCommand:
    def test_success(self) -> None:
        result = utils.run_command(["echo", "hello"])
        assert result.exitcode == 0
        assert any("hello" in line for line in result.stdout)

    def test_failure_raises(self) -> None:
        with pytest.raises(utils.CommandFailedException):
            utils.run_command(["false"])

    def test_failure_no_check(self) -> None:
        result = utils.run_command(["false"], check=False)
        assert result.exitcode != 0

    def test_failure_carries_stderr(self) -> None:
        with pytest.raises(utils.CommandFailedException) as exc:
            utils.run_command(["sh", "-c", "echo err >&2; exit 1"])
        assert exc.value.stderr == ["err"]


def _logged_pids(log: Path) -> list[int]:
    return [int(line) for line in log.read_text().split() if line.isdigit()]


class TestRunCommandInterrupted:
    def test_timeout_stops_and_reaps_the_command(self, tmp_path: Path) -> None:
        log = tmp_path / "run.log"
        start = time.monotonic()
        with utils.tee_output(log), pytest.raises(TimeoutError, match="did not finish within"):
            utils.run_command(["sh", "-c", "echo $$; exec sleep 30"], timeout=0.5)

        assert time.monotonic() - start < 5
        (pid,) = _logged_pids(log)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    def test_ctrl_c_stops_and_reaps_the_command(self, tmp_path: Path) -> None:
        log = tmp_path / "run.log"
        threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGINT)).start()
        with utils.tee_output(log), pytest.raises(KeyboardInterrupt):
            utils.run_command(["sh", "-c", "echo $$; exec sleep 30"])

        (pid,) = _logged_pids(log)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    def test_a_descendant_holding_the_pipes_cannot_outlast_the_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even once the command itself has exited."""
        monkeypatch.setattr(utils, "RELAY_DRAIN_SECONDS", 0.2)
        log = tmp_path / "run.log"
        start = time.monotonic()
        try:
            with utils.tee_output(log), pytest.raises(TimeoutError):
                utils.run_command(["sh", "-c", "sleep 30 & echo $!"], timeout=0.5)
            assert time.monotonic() - start < 5
        finally:
            for pid in _logged_pids(log):
                with contextlib.suppress(ProcessLookupError):  # already gone is the goal
                    os.kill(pid, signal.SIGKILL)

    @pytest.mark.parametrize("command", ["sleep 30", "sleep 30 & exec sleep 30"])
    def test_a_failed_stderr_relay_kills_the_command_and_raises(
        self, monkeypatch: pytest.MonkeyPatch, command: str
    ) -> None:
        """Including when a descendant holds the command's stdout open."""
        relay = utils._relay

        def broken_stderr(stream, color, capture) -> None:
            if color == "red":
                raise OSError("transcript disk full")
            relay(stream, color, capture)

        monkeypatch.setattr(utils, "_relay", broken_stderr)
        start = time.monotonic()
        with pytest.raises(OSError, match="transcript disk full"):
            utils.run_command(["sh", "-c", command])
        assert time.monotonic() - start < 5


class TestCompactConsole:
    """testrole.py's default output: tagged status lines on the terminal only."""

    def test_only_status_lines_reach_the_terminal(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        utils.use_compact_console("nginx", "lab:noble")
        log = tmp_path / "out.ansi"

        with utils.tee_output(log):
            utils.print_line("▶ converge")
            utils.run_command(["echo", "TASK [nginx : Install]"])
        utils._drain_stdout()

        terminal = capsys.readouterr().out
        assert "nginx lab:noble" in terminal
        assert "▶ converge" in terminal
        assert "TASK [nginx" not in terminal
        assert "$ echo" not in terminal
        assert "TASK [nginx : Install]" in log.read_text()

    def test_a_failure_without_a_task_shows_the_transcript_tail(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        utils.use_compact_console("nginx", "lab:noble")
        log = tmp_path / "out.ansi"
        failure_file = tmp_path / "failure.ansi"

        with utils.tee_output(log), pytest.raises(utils.CommandFailedException), utils.phase("converge"):
            utils.run_command(["sh", "-c", "echo 'ERROR! the playbook could not be found'; exit 2"])
        utils.report_failure([], log, failure_file)
        utils._drain_stdout()

        terminal = capsys.readouterr().out
        assert "✗ converge" in terminal
        assert "│ ERROR! the playbook could not be found" in terminal
        assert f"log: {log}" in terminal
        assert "ERROR! the playbook could not be found" in failure_file.read_text()

    def test_a_running_phase_is_plain_and_a_completed_one_green(self, capsys: pytest.CaptureFixture[str]) -> None:
        utils.use_compact_console("nginx", "lab:noble")

        with utils.phase("converge"):
            pass
        utils._drain_stdout()

        terminal = capsys.readouterr().out
        assert " ▶ converge\n" in terminal
        assert utils.colorize("✓ converge (0:00)", "green") in terminal

    def test_no_heartbeat_follows_the_result_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(utils, "PHASE_HEARTBEAT_SECONDS", 0.01)
        utils.use_compact_console("nginx", "lab:noble")
        beat = threading.Event()
        write_line = utils._write_line

        def noting_heartbeats(line: str, color: str | None, **kwargs: bool) -> None:
            write_line(line, color, **kwargs)
            if line.startswith("▶ converge ("):
                beat.set()

        monkeypatch.setattr(utils, "_write_line", noting_heartbeats)

        with utils.phase("converge"):
            assert beat.wait(5)
        # Heartbeats were due every 10ms; any that slipped past the result
        # line would show up in this window.
        time.sleep(0.2)
        utils._drain_stdout()

        lines = capsys.readouterr().out.splitlines()
        result = next(i for i, line in enumerate(lines) if "✓ converge" in line)
        assert any("▶ converge (" in line for line in lines[:result])
        assert not any("▶ converge (" in line for line in lines[result:])

    def test_on_a_terminal_the_result_overwrites_the_header(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        utils.use_compact_console("nginx", "lab:noble")
        monkeypatch.setattr(utils, "_REWRITABLE", True)
        log = tmp_path / "out.ansi"

        with utils.tee_output(log), utils.phase("converge"):
            pass
        utils._drain_stdout()

        # Split on newlines alone: the rewrite itself carries a carriage return.
        header, result = capsys.readouterr().out.rstrip("\n").split("\n")
        assert "▶ converge" in header
        assert result.startswith(utils._REWRITE_PREVIOUS_LINE)
        assert "✓ converge" in result
        assert utils._REWRITE_PREVIOUS_LINE not in log.read_text()

    def test_a_line_in_between_keeps_the_header(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        utils.use_compact_console("nginx", "lab:noble")
        monkeypatch.setattr(utils, "_REWRITABLE", True)

        with utils.phase("converge"):
            utils.print_line("Skipping base prerequisites")
        utils._drain_stdout()

        terminal = capsys.readouterr().out
        assert "▶ converge" in terminal
        assert "✓ converge" in terminal
        assert utils._REWRITE_PREVIOUS_LINE not in terminal

    def test_a_pipe_gets_appended_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        utils.use_compact_console("nginx", "lab:noble")

        with pytest.raises(RuntimeError), utils.phase("converge"):
            raise RuntimeError("boom")
        utils._drain_stdout()

        lines = capsys.readouterr().out.splitlines()
        assert "▶ converge" in lines[0]
        assert "✗ converge" in lines[1]
        assert utils._REWRITE_PREVIOUS_LINE not in "".join(lines)

    def test_on_a_terminal_the_heartbeat_overwrites_the_header(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(utils, "PHASE_HEARTBEAT_SECONDS", 0.01)
        utils.use_compact_console("nginx", "lab:noble")
        monkeypatch.setattr(utils, "_REWRITABLE", True)

        with utils.phase("converge"):
            time.sleep(0.1)
        utils._drain_stdout()

        # Every line after the header, heartbeats and result alike, rewrote
        # the one before it.
        header, *rest = capsys.readouterr().out.rstrip("\n").split("\n")
        assert "▶ converge" in header
        assert len(rest) > 1
        assert all(line.startswith(utils._REWRITE_PREVIOUS_LINE) for line in rest)
        assert "✓ converge" in rest[-1]


class TestFailedTask:
    """failed_tasks: the block of ansible-playbook output for each task that failed."""

    def test_keeps_the_failed_item_and_drops_what_follows(self) -> None:
        stdout = [
            "TASK [web : Root listing serves] ***********************************************",
            "ok: [lab] => ",
            "    status: 200",
            "",
            "\x1b[0;31mTASK [web : Per-tree listing renders] *******************************\x1b[0m",
            "Friday 09 October 2026  10:19:04 +0200 (0:00:00.039)       0:00:01.134 ********",
            "ok: [lab] (item=docs) => ",
            "    status: 200",
            "\x1b[0;31mfailed: [lab] (item=home) => \x1b[0m",
            "    content: |-",
            "        <html>",
            "",
            "        </html>",
            "    msg: 'Status code was 403 and not [200]'",
            "skipping: [lab] => (item=data)  => ",
            "    skip_reason: Conditional result was False",
            "",
            "PLAY RECAP *********************************************************************",
            "lab                        : ok=5    changed=0    unreachable=0    failed=1",
        ]

        assert utils.failed_tasks(stdout) == [[stdout[4], stdout[5], *stdout[8:14]]]

    def test_an_ignored_failure_does_not_count(self) -> None:
        stdout = [
            "TASK [web : Probe] *************************************************************",
            "fatal: [lab]: FAILED! => ",
            "    msg: optional",
            "...ignoring",
            "TASK [web : Next] **************************************************************",
            "ok: [lab]",
        ]

        assert utils.failed_tasks(stdout) == []

    def test_an_ignored_loop_drops_all_its_failed_items(self) -> None:
        stdout = [
            "TASK [web : Probe] *************************************************************",
            "failed: [lab] (item=a) => ",
            "    msg: optional",
            "failed: [lab] (item=b) => ",
            "    msg: optional",
            "...ignoring",
            "TASK [web : Next] **************************************************************",
            "ok: [lab]",
        ]

        assert utils.failed_tasks(stdout) == []

    def test_a_rescued_failure_is_kept_beside_the_last(self) -> None:
        stdout = [
            "TASK [web : Try] ***************************************************************",
            "fatal: [lab]: FAILED! => ",
            "    msg: rescued",
            "TASK [web : Rescue] ************************************************************",
            "ok: [lab]",
            "TASK [web : Later] *************************************************************",
            "fatal: [lab]: FAILED! => ",
            "    msg: real",
            "",
        ]

        assert utils.failed_tasks(stdout) == [stdout[0:3], stdout[5:8]]

    def test_task_title_drops_colour_and_stars(self) -> None:
        header = "\x1b[0;31mTASK [web : Probe] [CHECK MODE] ******************************\x1b[0m"

        assert utils.task_title(header) == "TASK [web : Probe] [CHECK MODE]"


# ---------------------------------------------------------------------------
# sleep_tick
# ---------------------------------------------------------------------------


class TestSleepTick:
    def test_sub_second_polls_emit_one_dot_per_second(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = [100.0]
        dots: list[str] = []
        monkeypatch.setattr(utils, "_LAST_TICK", 0.0)
        monkeypatch.setattr(utils.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(utils.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
        monkeypatch.setattr(utils, "_emit", dots.append)

        for _ in range(25):
            utils.sleep_tick(0.1)

        # 2.5s of polling: dots at t=0, ~1 and ~2.
        assert dots == ["."] * 3
        assert clock[0] == pytest.approx(102.5)
