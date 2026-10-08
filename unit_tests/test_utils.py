"""Unit tests for test/utils.py — tee_output, print, and process helpers."""

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

    def test_roles_keep_their_colour(self) -> None:
        utils.use_compact_console("nginx", "lab:noble")
        first = utils._CONSOLE_TAG
        utils.use_compact_console("nginx", "minimal:noble")
        assert first is not None
        assert utils._CONSOLE_TAG is not None
        assert first.split("m", 1)[0] == utils._CONSOLE_TAG.split("m", 1)[0]
