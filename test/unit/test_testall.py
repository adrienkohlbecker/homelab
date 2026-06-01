"""Unit tests for test/testall.py — joblog I/O, merge logic, result types."""

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import testall


# ---------------------------------------------------------------------------
# MachineRole
# ---------------------------------------------------------------------------


class TestMachineRole:
    def test_fields(self) -> None:
        mr = testall.MachineRole("box", "jammy", "nginx")
        assert mr.machine == "box"
        assert mr.ubuntu_name == "jammy"
        assert mr.role == "nginx"

    def test_hashable(self) -> None:
        mr = testall.MachineRole("box", "jammy", "nginx")
        assert {mr: 1}[mr] == 1

    def test_equality(self) -> None:
        a = testall.MachineRole("box", "jammy", "nginx")
        b = testall.MachineRole("box", "jammy", "nginx")
        assert a == b


# ---------------------------------------------------------------------------
# JobResult
# ---------------------------------------------------------------------------


class TestJobResult:
    def test_frozen(self) -> None:
        jr = testall.JobResult("box", "jammy", "nginx", 12.5, 0, "2026-01-01T00:00:00Z")
        with pytest.raises(AttributeError):
            jr.exitval = 1  # type: ignore[misc]

    def test_peak_kb_default(self) -> None:
        jr = testall.JobResult("box", "jammy", "nginx", 0.0, 0, "")
        assert jr.peak_kb == 0


# ---------------------------------------------------------------------------
# _cancelled_result
# ---------------------------------------------------------------------------


class TestCancelledResult:
    def test_exitval_is_130(self) -> None:
        mr = testall.MachineRole("box", "jammy", "nginx")
        r = testall._cancelled_result(mr)
        assert r.exitval == 130
        assert r.runtime == 0.0
        assert r.started_at == ""

    def test_carries_triple(self) -> None:
        mr = testall.MachineRole("lab", "noble", "podman")
        r = testall._cancelled_result(mr)
        assert r.machine == "lab"
        assert r.ubuntu_name == "noble"
        assert r.role == "podman"


# ---------------------------------------------------------------------------
# _merge_with_prior
# ---------------------------------------------------------------------------


class TestMergeWithPrior:
    def test_new_results_overwrite_prior(self) -> None:
        mr = testall.MachineRole("box", "jammy", "nginx")
        prior_jr = testall.JobResult("box", "jammy", "nginx", 10.0, 1, "old")
        new_jr = testall.JobResult("box", "jammy", "nginx", 5.0, 0, "new")
        merged = testall._merge_with_prior([new_jr], {mr: prior_jr})
        assert len(merged) == 1
        assert merged[0].exitval == 0
        assert merged[0].started_at == "new"

    def test_keeps_prior_for_unrun_triples(self) -> None:
        mr1 = testall.MachineRole("box", "jammy", "nginx")
        mr2 = testall.MachineRole("box", "jammy", "podman")
        prior = {
            mr1: testall.JobResult("box", "jammy", "nginx", 10.0, 0, "old"),
            mr2: testall.JobResult("box", "jammy", "podman", 8.0, 0, "old"),
        }
        new = [testall.JobResult("box", "jammy", "nginx", 5.0, 0, "new")]
        merged = testall._merge_with_prior(new, prior)
        assert len(merged) == 2
        roles = {r.role for r in merged}
        assert roles == {"nginx", "podman"}

    def test_adds_new_triples(self) -> None:
        prior: dict[testall.MachineRole, testall.JobResult] = {}
        new = [testall.JobResult("box", "jammy", "zfs", 3.0, 0, "new")]
        merged = testall._merge_with_prior(new, prior)
        assert len(merged) == 1
        assert merged[0].role == "zfs"

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
            testall.JobResult("box", "jammy", "nginx", 12.345, 0, "2026-01-01T00:00:00Z", 512000),
            testall.JobResult("lab", "noble", "podman", 60.0, 1, "2026-01-01T01:00:00Z", 0),
        ]
        testall._write_joblog(results)
        prior = testall._read_prior_results()
        assert len(prior) == 2
        mr1 = testall.MachineRole("box", "jammy", "nginx")
        assert prior[mr1].exitval == 0
        assert prior[mr1].peak_kb == 512000
        mr2 = testall.MachineRole("lab", "noble", "podman")
        assert prior[mr2].exitval == 1

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
            w.writerow({"Role": "nginx", "Ubuntu": "jammy", "Machine": "box", "Runtime": "10", "Exitval": "0", "PeakKB": "0", "Started": ""})
            w.writerow({"Role": "podman", "Ubuntu": "jammy", "Machine": "box", "Runtime": "20", "Exitval": "1", "PeakKB": "0", "Started": ""})
            w.writerow({"Role": "zfs", "Ubuntu": "noble", "Machine": "lab", "Runtime": "30", "Exitval": "125", "PeakKB": "0", "Started": ""})
        failed = testall.get_failed_roles()
        assert len(failed) == 2
        assert testall.MachineRole("box", "jammy", "podman") in failed
        assert testall.MachineRole("lab", "noble", "zfs") in failed

    def test_empty_when_all_pass(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "out.tsv"
        monkeypatch.setattr(testall, "LOG_FILE", log)
        with log.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=testall.JOBLOG_FIELDS, delimiter="\t")
            w.writeheader()
            w.writerow({"Role": "nginx", "Ubuntu": "jammy", "Machine": "box", "Runtime": "10", "Exitval": "0", "PeakKB": "0", "Started": ""})
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
        plan = [testall.MachineRole("box", "jammy", "nginx")]
        testall.setup_output_dir(plan)
        assert not stale.exists()
        assert other.exists()


# ---------------------------------------------------------------------------
# TESTROLE_OWNED_FLAGS
# ---------------------------------------------------------------------------


class TestTestRoleOwnedFlags:
    def test_machine_is_owned(self) -> None:
        assert "--machine" in testall.TESTROLE_OWNED_FLAGS

    def test_keep_is_owned(self) -> None:
        assert "--keep" in testall.TESTROLE_OWNED_FLAGS

    def test_ubuntu_is_owned(self) -> None:
        assert "--ubuntu" in testall.TESTROLE_OWNED_FLAGS

    def test_random_flag_not_owned(self) -> None:
        assert "--verbose" not in testall.TESTROLE_OWNED_FLAGS
