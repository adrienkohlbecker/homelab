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

    def test_restores_previous_state(self, tmp_path: Path) -> None:
        assert utils._OUTPUT_LOG is None
        with utils.tee_output(tmp_path / "a.log"):
            assert utils._OUTPUT_LOG is not None
        assert utils._OUTPUT_LOG is None

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        log_path = tmp_path / "sub" / "dir" / "test.log"
        with utils.tee_output(log_path):
            utils._emit("x")
        assert log_path.exists()


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

    def test_empty_stderr(self) -> None:
        exc = utils.CommandFailedException(["ls"], 1, [])
        assert "1" in str(exc)


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

    def test_a_failed_phase_shows_its_failure_and_where_the_log_is(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        utils.use_compact_console("nginx", "lab:noble")
        log = tmp_path / "out.ansi"

        with utils.tee_output(log), pytest.raises(utils.CommandFailedException), utils.phase("converge"):
            utils.run_command(["sh", "-c", "echo 'fatal: [lab]: FAILED!'; exit 2"])
        utils.print_log_tail(log)
        utils._drain_stdout()

        terminal = capsys.readouterr().out
        assert "✗ converge" in terminal
        assert "│ fatal: [lab]: FAILED!" in terminal
        assert f"log: {log}" in terminal
        assert "fatal: [lab]: FAILED!" in log.read_text()

    def test_no_heartbeat_follows_the_result_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(utils, "PHASE_HEARTBEAT_SECONDS", 0.01)
        utils.use_compact_console("nginx", "lab:noble")
        beat = threading.Event()
        print_line = utils.print_line

        def noting_heartbeats(line: str, error: bool = False) -> None:
            print_line(line, error)
            if "still running" in line:
                beat.set()

        monkeypatch.setattr(utils, "print_line", noting_heartbeats)

        with utils.phase("converge"):
            assert beat.wait(5)
        # Heartbeats were due every 10ms; any that slipped past the result
        # line would show up in this window.
        time.sleep(0.2)
        utils._drain_stdout()

        lines = capsys.readouterr().out.splitlines()
        result = next(i for i, line in enumerate(lines) if "✓ converge" in line)
        assert any("still running" in line for line in lines[:result])
        assert not any("still running" in line for line in lines[result:])

    def test_roles_keep_their_colour(self) -> None:
        utils.use_compact_console("nginx", "lab:noble")
        first = utils._CONSOLE_TAG
        utils.use_compact_console("nginx", "minimal:noble")
        assert first is not None
        assert utils._CONSOLE_TAG is not None
        assert first.split("m", 1)[0] == utils._CONSOLE_TAG.split("m", 1)[0]
