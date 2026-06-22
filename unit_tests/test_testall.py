"""Unit tests for test/testall.py — joblog I/O and result types."""

from pathlib import Path

import pytest
import testall

# ---------------------------------------------------------------------------
# JobResult
# ---------------------------------------------------------------------------


class TestJobResult:
    def test_frozen(self) -> None:
        jr = testall.JobResult(testall.TestCell("box", "jammy", "nginx"), 12.5, 0, "2026-01-01T00:00:00Z")
        with pytest.raises(AttributeError):
            jr.exitval = 1  # type: ignore[misc]

    def test_peak_kb_default(self) -> None:
        jr = testall.JobResult(testall.TestCell("box", "jammy", "nginx"), 0.0, 0, "")
        assert jr.peak_kb == 0


# ---------------------------------------------------------------------------
# _cancelled_result
# ---------------------------------------------------------------------------


class TestCancelledResult:
    def test_exitval_is_130(self) -> None:
        r = testall._cancelled_result(testall.TestCell("box", "jammy", "nginx"))
        assert r.exitval == 130
        assert r.runtime == 0.0
        assert r.started_at == ""

    def test_carries_triple(self) -> None:
        r = testall._cancelled_result(testall.TestCell("lab", "noble", "podman"))
        assert r.cell == testall.TestCell("lab", "noble", "podman")


# ---------------------------------------------------------------------------
# _write_joblog / _read_joblog round-trip
# ---------------------------------------------------------------------------


class TestJoblogRoundTrip:
    def test_write_then_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "out.tsv"
        monkeypatch.setattr(testall, "LOG_FILE", log)
        results = [
            testall.JobResult(testall.TestCell("box", "jammy", "nginx"), 12.345, 0, "2026-01-01T00:00:00Z", 512000),
            testall.JobResult(testall.TestCell("lab", "noble", "podman"), 60.0, 1, "2026-01-01T01:00:00Z", 0),
        ]
        testall._write_joblog(results)
        prior = testall._read_joblog()
        assert len(prior) == 2
        assert prior[0].cell == testall.TestCell("box", "jammy", "nginx")
        assert prior[0].exitval == 0
        assert prior[0].peak_kb == 512000
        assert prior[1].cell == testall.TestCell("lab", "noble", "podman")
        assert prior[1].exitval == 1

    def test_read_missing_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(testall, "LOG_FILE", tmp_path / "nonexistent.tsv")
        assert testall._read_joblog() == []


# ---------------------------------------------------------------------------
# setup_output_dir
# ---------------------------------------------------------------------------


class TestSetupOutputDir:
    def test_clears_stale_ansi_for_plan(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        out = tmp_path / "out"
        monkeypatch.setattr(testall, "OUT_DIR", out)
        out.mkdir()
        stale_files = [
            out / f"box.jammy.nginx.{suffix}.ansi"
            for suffix in ("output", "journal", "boot", "dmesg", "systemctl-failed")
        ]
        for stale in stale_files:
            stale.write_text("old")
        other = out / "lab.noble.podman.output.ansi"
        other.write_text("keep")
        plan = [testall.TestCell("box", "jammy", "nginx")]
        testall.setup_output_dir(plan)
        assert all(not stale.exists() for stale in stale_files)
        assert other.exists()
