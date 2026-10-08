"""test:all drives real GNU parallel over stand-in cells."""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ALL_SH = Path(__file__).resolve().parents[1] / "mise-tasks" / "test" / "all.sh"
ROLES = ("alpha", "bravo", "charlie")

pytestmark = pytest.mark.skipif(shutil.which("parallel") is None, reason="needs GNU parallel")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A checkout whose matrix lists three cells and whose testrole.py fails
    any role named in failing.txt, logging each invocation's arguments."""
    (tmp_path / "test").mkdir()
    matrix = "".join(f"lab\tnoble\t{role}\n" for role in ROLES)
    (tmp_path / "test" / "matrix.py").write_text(f"#!/bin/sh\nprintf '{matrix.encode('unicode_escape').decode()}'\n")
    (tmp_path / "test" / "testrole.py").write_text(
        '#!/bin/sh\necho "$*" >>calls.txt\nrole=$5\ngrep -qx "$role" failing.txt 2>/dev/null && exit 1\nexit 0\n'
    )
    for script in ("matrix.py", "testrole.py"):
        (tmp_path / "test" / script).chmod(0o755)
    return tmp_path


def run(repo: Path, *args: str, failing: tuple[str, ...] = (), retry: bool = False, jobs: str = "2"):
    """Invoke all.sh as `mise run test:all [--retry-failed] [-- args...]` does:
    mise passes every task argument in "$@", its own flags included, and
    the forwarded ones alone, shell-quoted, in usage_testrole_args."""
    (repo / "failing.txt").write_text("".join(f"{role}\n" for role in failing))
    (repo / "calls.txt").unlink(missing_ok=True)
    env = {
        **os.environ,
        "usage_jobs": jobs,
        "usage_retry_failed": str(retry).lower(),
        "usage_testrole_args": shlex.join(args),
    }
    argv = [*(["--retry-failed"] if retry else []), *(["--", *args] if args else [])]
    result = subprocess.run(["bash", str(ALL_SH), *argv], cwd=repo, env=env, text=True, capture_output=True)
    calls = (repo / "calls.txt").read_text().splitlines() if (repo / "calls.txt").exists() else []
    return result, calls


def failed_roles(stdout: str) -> list[str]:
    return [line.split()[5] for line in stdout.splitlines() if line.startswith("  test/testrole.py")]


def test_retry_reruns_failures_and_reports_only_current_ones(repo: Path) -> None:
    result, _ = run(repo, failing=("bravo", "charlie"))
    assert result.returncode != 0
    assert failed_roles(result.stdout) == ["bravo", "charlie"]

    result, calls = run(repo, failing=("charlie",), retry=True)

    assert result.returncode != 0
    assert sorted(call.split()[4] for call in calls) == ["bravo", "charlie"]
    assert not any("--retry-failed" in call for call in calls)
    assert failed_roles(result.stdout) == ["charlie"]


def test_retry_starts_cells_an_interrupt_skipped(repo: Path) -> None:
    run(repo)
    joblog = repo / "test" / "out.tsv"
    # Keep only alpha's row (Seq 1), as if an interrupt stopped the rest.
    header, *rows = joblog.read_text().splitlines(keepends=True)
    joblog.write_text(header + next(row for row in rows if row.split("\t")[0] == "1"))

    result, calls = run(repo, retry=True)

    assert result.returncode == 0, result.stderr
    assert sorted(call.split()[4] for call in calls) == ["bravo", "charlie"]


def test_retry_sets_aside_an_old_format_joblog(repo: Path) -> None:
    joblog = repo / "test" / "out.tsv"
    joblog.write_text("Role\tUbuntu\tMachine\tRuntime\tExitval\tStarted\nalpha\tnoble\tlab\t1\t1\t0\n")

    result, calls = run(repo, retry=True)

    assert result.returncode == 0, result.stderr
    assert len(calls) == len(ROLES)
    assert (repo / "test" / "out.tsv.legacy").exists()


def test_forwards_extra_arguments_to_every_cell(repo: Path) -> None:
    result, calls = run(repo, "--upstream-mirrors", "-e", "a b")

    assert result.returncode == 0, result.stderr
    assert len(calls) == len(ROLES)
    assert all(call.endswith(" --upstream-mirrors -e a b") for call in calls)


@pytest.mark.parametrize("jobs", ["0", "-1", "many"])
def test_rejects_a_non_positive_job_count(repo: Path, jobs: str) -> None:
    result, calls = run(repo, jobs=jobs)

    assert result.returncode != 0
    assert "--jobs must be a positive number" in result.stderr
    assert calls == []
