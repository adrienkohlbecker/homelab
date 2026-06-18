"""Unit tests for test/utils.py — colorize, tee_output, print helpers."""

import asyncio
import signal
from pathlib import Path

import pytest

import utils


# ---------------------------------------------------------------------------
# colorize
# ---------------------------------------------------------------------------


class TestColorize:
    def test_known_color(self) -> None:
        result = utils.colorize("hello", "red")
        assert "\033[0;41m" in result
        assert "hello" in result
        assert "\033[0m" in result

    def test_cyan(self) -> None:
        result = utils.colorize("test", "cyan")
        assert "\033[0;36m" in result

    def test_unknown_color_passthrough(self) -> None:
        assert utils.colorize("hello", "magenta") == "hello"

    def test_none_color_passthrough(self) -> None:
        assert utils.colorize("hello", None) == "hello"


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
# terminate_subprocess
# ---------------------------------------------------------------------------


class TestTerminateSubprocess:
    def test_immediate_kill(self) -> None:
        async def _run() -> None:
            proc = await asyncio.create_subprocess_exec(
                "sleep",
                "60",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await utils.terminate_subprocess(proc)
            assert proc.returncode is not None

        asyncio.run(_run())

    def test_grace_period_with_sigint(self) -> None:
        async def _run() -> None:
            proc = await asyncio.create_subprocess_exec(
                "sleep",
                "60",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await utils.terminate_subprocess(
                proc,
                grace_seconds=1.0,
                initial_signal=signal.SIGINT,
            )
            assert proc.returncode is not None

        asyncio.run(_run())

    def test_zero_grace_non_sigkill_raises(self) -> None:
        async def _run() -> None:
            proc = await asyncio.create_subprocess_exec(
                "true",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
            with pytest.raises(ValueError, match="grace_seconds must be > 0"):
                await utils.terminate_subprocess(
                    proc,
                    grace_seconds=0,
                    initial_signal=signal.SIGINT,
                )

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# CommandResult
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# run_command
# ---------------------------------------------------------------------------


class TestRunCommand:
    def test_success(self) -> None:
        async def _run() -> None:
            result = await utils.run_command(["echo", "hello"], quiet=True)
            assert result.exitcode == 0
            assert any("hello" in line for line in result.stdout)

        asyncio.run(_run())

    def test_failure_raises(self) -> None:
        async def _run() -> None:
            with pytest.raises(utils.CommandFailedException):
                await utils.run_command(["false"], quiet=True)

        asyncio.run(_run())

    def test_failure_no_check(self) -> None:
        async def _run() -> None:
            result = await utils.run_command(["false"], check=False, quiet=True)
            assert result.exitcode != 0

        asyncio.run(_run())

    def test_captures_stderr(self) -> None:
        async def _run() -> None:
            result = await utils.run_command(["sh", "-c", "echo err >&2"], check=False, quiet=True)
            assert any("err" in line for line in result.stderr)

        asyncio.run(_run())


class TestCommandResult:
    def test_named_fields(self) -> None:
        r = utils.CommandResult(exitcode=0, stdout=["ok"], stderr=[])
        assert r.exitcode == 0
        assert r.stdout == ["ok"]
        assert r.stderr == []

    def test_tuple_unpacking(self) -> None:
        exitcode, stdout, stderr = utils.CommandResult(1, ["a"], ["b"])
        assert exitcode == 1
