"""Unit tests for test/utils.py — tee_output, print, and process helpers."""

import asyncio
import signal
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
            await utils.terminate_subprocess(proc, grace_seconds=1.0)
            assert proc.returncode == -signal.SIGINT

        asyncio.run(_run())

    def test_grace_escalates_to_sigkill(self) -> None:
        async def _run() -> None:
            proc = await asyncio.create_subprocess_exec("sh", "-c", "trap '' INT; sleep 60")
            await asyncio.sleep(0.2)
            await utils.terminate_subprocess(proc, grace_seconds=0.5)
            assert proc.returncode == -signal.SIGKILL

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# run_command
# ---------------------------------------------------------------------------


class TestRunCommand:
    def test_success(self) -> None:
        result = asyncio.run(utils.run_command(["echo", "hello"]))
        assert result.exitcode == 0
        assert any("hello" in line for line in result.stdout)

    def test_failure_raises(self) -> None:
        with pytest.raises(utils.CommandFailedException):
            asyncio.run(utils.run_command(["false"]))

    def test_failure_no_check(self) -> None:
        result = asyncio.run(utils.run_command(["false"], check=False))
        assert result.exitcode != 0

    def test_failure_carries_stderr(self) -> None:
        with pytest.raises(utils.CommandFailedException) as exc:
            asyncio.run(utils.run_command(["sh", "-c", "echo err >&2; exit 1"]))
        assert exc.value.stderr == ["err"]
