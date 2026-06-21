"""Unit tests for test/testall.py — joblog I/O, merge logic, result types."""

import csv
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
# _merge_with_prior
# ---------------------------------------------------------------------------


class TestMergeWithPrior:
    def test_new_results_overwrite_prior(self) -> None:
        cell = testall.TestCell("box", "jammy", "nginx")
        prior_jr = testall.JobResult(cell, 10.0, 1, "old")
        new_jr = testall.JobResult(cell, 5.0, 0, "new")
        merged = testall._merge_with_prior([new_jr], {cell: prior_jr})
        assert len(merged) == 1
        assert merged[0].exitval == 0
        assert merged[0].started_at == "new"

    def test_keeps_prior_for_unrun_triples(self) -> None:
        cell1 = testall.TestCell("box", "jammy", "nginx")
        cell2 = testall.TestCell("box", "jammy", "podman")
        prior = {
            cell1: testall.JobResult(cell1, 10.0, 0, "old"),
            cell2: testall.JobResult(cell2, 8.0, 0, "old"),
        }
        new = [testall.JobResult(cell1, 5.0, 0, "new")]
        merged = testall._merge_with_prior(new, prior)
        assert len(merged) == 2
        roles = {r.cell.role for r in merged}
        assert roles == {"nginx", "podman"}

    def test_adds_new_triples(self) -> None:
        prior: dict[testall.TestCell, testall.JobResult] = {}
        cell = testall.TestCell("box", "jammy", "zfs")
        new = [testall.JobResult(cell, 3.0, 0, "new")]
        merged = testall._merge_with_prior(new, prior)
        assert len(merged) == 1
        assert merged[0].cell.role == "zfs"

    def test_empty_both(self) -> None:
        assert testall._merge_with_prior([], {}) == []


# ---------------------------------------------------------------------------
# _write_joblog / _read_prior_results round-trip
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
        prior = testall._read_prior_results()
        assert len(prior) == 2
        cell1 = testall.TestCell("box", "jammy", "nginx")
        assert prior[cell1].exitval == 0
        assert prior[cell1].peak_kb == 512000
        cell2 = testall.TestCell("lab", "noble", "podman")
        assert prior[cell2].exitval == 1

    def test_read_missing_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(testall, "LOG_FILE", tmp_path / "nonexistent.tsv")
        assert testall._read_prior_results() == {}


# ---------------------------------------------------------------------------
# _rotate_joblog
# ---------------------------------------------------------------------------


class TestRotateJoblog:
    def test_rotates_to_prev(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "out.tsv"
        prev = tmp_path / "out.tsv.prev"
        monkeypatch.setattr(testall, "LOG_FILE", log)
        monkeypatch.setattr(testall, "LOG_FILE_PREV", prev)
        log.write_text("content1")
        testall._rotate_joblog()
        assert not log.exists()
        assert prev.read_text() == "content1"

    def test_overwrites_old_prev(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "out.tsv"
        prev = tmp_path / "out.tsv.prev"
        monkeypatch.setattr(testall, "LOG_FILE", log)
        monkeypatch.setattr(testall, "LOG_FILE_PREV", prev)
        prev.write_text("old")
        log.write_text("new")
        testall._rotate_joblog()
        assert prev.read_text() == "new"

    def test_noop_when_no_log(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(testall, "LOG_FILE", tmp_path / "absent.tsv")
        monkeypatch.setattr(testall, "LOG_FILE_PREV", tmp_path / "absent.tsv.prev")
        testall._rotate_joblog()


# ---------------------------------------------------------------------------
# get_failed_roles
# ---------------------------------------------------------------------------


class TestGetFailedRoles:
    def test_returns_failures(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "out.tsv"
        monkeypatch.setattr(testall, "LOG_FILE", log)
        with log.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=testall.JOBLOG_FIELDS, delimiter="\t")
            w.writeheader()
            w.writerow(
                {
                    "Role": "nginx",
                    "Ubuntu": "jammy",
                    "Machine": "box",
                    "Runtime": "10",
                    "Exitval": "0",
                    "PeakKB": "0",
                    "Started": "",
                }
            )
            w.writerow(
                {
                    "Role": "podman",
                    "Ubuntu": "jammy",
                    "Machine": "box",
                    "Runtime": "20",
                    "Exitval": "1",
                    "PeakKB": "0",
                    "Started": "",
                }
            )
            w.writerow(
                {
                    "Role": "zfs",
                    "Ubuntu": "noble",
                    "Machine": "lab",
                    "Runtime": "30",
                    "Exitval": "125",
                    "PeakKB": "0",
                    "Started": "",
                }
            )
        failed = testall.get_failed_roles()
        assert len(failed) == 2
        assert testall.TestCell("box", "jammy", "podman") in failed
        assert testall.TestCell("lab", "noble", "zfs") in failed

    def test_empty_when_all_pass(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "out.tsv"
        monkeypatch.setattr(testall, "LOG_FILE", log)
        with log.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=testall.JOBLOG_FIELDS, delimiter="\t")
            w.writeheader()
            w.writerow(
                {
                    "Role": "nginx",
                    "Ubuntu": "jammy",
                    "Machine": "box",
                    "Runtime": "10",
                    "Exitval": "0",
                    "PeakKB": "0",
                    "Started": "",
                }
            )
        assert testall.get_failed_roles() == []

    def test_missing_log(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(testall, "LOG_FILE", tmp_path / "nope.tsv")
        assert testall.get_failed_roles() == []


# ---------------------------------------------------------------------------
# setup_output_dir
# ---------------------------------------------------------------------------


class TestSetupOutputDir:
    def test_clears_stale_ansi_for_plan(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        out = tmp_path / "out"
        monkeypatch.setattr(testall, "OUT_DIR", out)
        out.mkdir()
        stale = out / "box.jammy.nginx.output.ansi"
        stale.write_text("old")
        other = out / "lab.noble.podman.output.ansi"
        other.write_text("keep")
        plan = [testall.TestCell("box", "jammy", "nginx")]
        testall.setup_output_dir(plan)
        assert not stale.exists()
        assert other.exists()
