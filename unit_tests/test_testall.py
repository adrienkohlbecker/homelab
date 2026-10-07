"""Unit tests for test/testall.py — joblog I/O and result types."""

import asyncio
from pathlib import Path
from typing import cast

import pytest
import testall


def test_parallel_role_child_gets_private_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_kwargs: dict[str, object] = {}

    class Stdout:
        async def readline(self) -> bytes:
            return b""

    class Process:
        def __init__(self) -> None:
            self.returncode: int | None = 0
            self.stdout = cast(asyncio.StreamReader, Stdout())

        async def wait(self) -> int:
            return 0

    async def create_subprocess_exec(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
        captured_kwargs.update(kwargs)
        return cast(asyncio.subprocess.Process, Process())

    monkeypatch.setattr(testall.asyncio, "create_subprocess_exec", create_subprocess_exec)

    asyncio.run(
        testall._run_role(
            1,
            testall.TestCell("lab", "noble", "test"),
            asyncio.Semaphore(1),
        )
    )

    assert captured_kwargs["stdin"] == asyncio.subprocess.DEVNULL


# ---------------------------------------------------------------------------
# _cancelled_result
# ---------------------------------------------------------------------------


class TestCancelledResult:
    def test_returns_cancelled_job(self) -> None:
        cell = testall.TestCell("lab", "noble", "nginx")
        assert testall._cancelled_result(cell) == testall.JobResult(cell, 0.0, 130, "")


# ---------------------------------------------------------------------------
# _write_joblog / _read_joblog round-trip
# ---------------------------------------------------------------------------


class TestJoblogRoundTrip:
    def test_write_then_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "out.tsv"
        monkeypatch.setattr(testall, "LOG_FILE", log)
        results = [
            testall.JobResult(testall.TestCell("lab", "noble", "nginx"), 12.345, 0, "2026-01-01T00:00:00Z"),
            testall.JobResult(testall.TestCell("lab", "noble", "podman"), 60.0, 1, "2026-01-01T01:00:00Z"),
        ]
        testall._write_joblog(results)
        assert testall._read_joblog() == results

    def test_read_missing_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(testall, "LOG_FILE", tmp_path / "nonexistent.tsv")
        assert testall._read_joblog() == []


def test_retry_failed_reruns_failures_and_updates_them_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(testall, "LOG_FILE", tmp_path / "out.tsv")
    passed = testall.JobResult(testall.TestCell("lab", "noble", "nginx"), 1.0, 0, "t0")
    failed = testall.JobResult(testall.TestCell("lab", "noble", "podman"), 2.0, 1, "t0")
    testall._write_joblog([passed, failed])
    rerun: list[testall.TestCell] = []

    async def run_all(cells: list[testall.TestCell], jobs: int) -> tuple[list[testall.JobResult], bool]:
        rerun.extend(cells)
        return [testall.JobResult(cell, 3.0, 0, "t1") for cell in cells], False

    monkeypatch.setattr(testall, "run_all", run_all)
    monkeypatch.setattr(testall.sys, "argv", ["testall.py", "--retry-failed"])

    assert testall.main() == 0
    assert rerun == [failed.cell]
    assert testall._read_joblog() == [passed, testall.JobResult(failed.cell, 3.0, 0, "t1")]
