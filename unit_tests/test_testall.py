"""Unit tests for test/testall.py — joblog I/O and result types."""

import argparse
import asyncio
from pathlib import Path
from typing import cast

import pytest
import testall


def test_comma_separated_normalizes_values() -> None:
    parse = testall._comma_separated()

    assert parse(" nginx, podman,nginx ") == frozenset({"nginx", "podman"})


def test_comma_separated_rejects_empty_values() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="at least one value"):
        testall._comma_separated()(" , ")


def test_comma_separated_rejects_unknown_choices() -> None:
    parse = testall._comma_separated(choices=("lab", "minimal"), label="machine profile")

    with pytest.raises(argparse.ArgumentTypeError, match=r"unknown machine profile\(s\): pug"):
        parse("lab,pug")


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
            [],
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
