"""Integration tests for the Codex branch setup in worktree/populate.sh."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
POPULATE_SH = REPO_ROOT / "mise-tasks" / "worktree" / "populate.sh"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


class Sandbox:
    """A throwaway repository with a Codex-shaped detached worktree."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        self.wt = root / "codex_home" / "worktrees" / "a1b2" / "repo"
        self.env = dict(os.environ)
        self.env.update(
            HOME=str(root / "home"),
            PATH="/usr/bin:/bin",
            GIT_CONFIG_GLOBAL="/dev/null",
            GIT_CONFIG_SYSTEM="/dev/null",
            GIT_AUTHOR_NAME="Test",
            GIT_AUTHOR_EMAIL="test@example.com",
            GIT_COMMITTER_NAME="Test",
            GIT_COMMITTER_EMAIL="test@example.com",
        )
        for leak in ("GIT_EDITOR", "EDITOR", "VISUAL", "GIT_DIR", "GIT_WORK_TREE"):
            self.env.pop(leak, None)

        (root / "home").mkdir()
        self.git("init", "-q", "-b", "master", str(self.repo), cwd=root)
        for directory in ("packer", "terraform"):
            path = self.repo / directory
            path.mkdir()
            (path / ".gitkeep").touch()
        self.git("add", ".", cwd=self.repo)
        self.git("commit", "-q", "-m", "base", cwd=self.repo)
        self.git("worktree", "add", "-q", "--detach", str(self.wt), "HEAD", cwd=self.repo)

    def git(self, *args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["git", *args], cwd=cwd, env=self.env, text=True, capture_output=True)
        if check and result.returncode != 0:
            raise AssertionError(result.stderr)
        return result

    def run_populate(self, *, codex: bool = True) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        if codex:
            env["CODEX_WORKTREE_PATH"] = str(self.wt)
        else:
            env.pop("CODEX_WORKTREE_PATH", None)
        return subprocess.run(
            ["bash", str(POPULATE_SH), str(self.wt)],
            cwd=self.repo,
            env=env,
            text=True,
            capture_output=True,
        )


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def test_codex_worktree_gets_shared_branch(sandbox: Sandbox) -> None:
    head = sandbox.git("rev-parse", "HEAD", cwd=sandbox.wt).stdout.strip()

    result = sandbox.run_populate()

    assert result.returncode == 0, result.stderr
    assert sandbox.git("branch", "--show-current", cwd=sandbox.wt).stdout.strip() == "codex/a1b2"
    assert sandbox.git("rev-parse", "codex/a1b2", cwd=sandbox.repo).stdout.strip() == head

    rerun = sandbox.run_populate()
    assert rerun.returncode == 0, rerun.stderr
    assert sandbox.git("branch", "--show-current", cwd=sandbox.wt).stdout.strip() == "codex/a1b2"


def test_non_codex_detached_worktree_stays_detached(sandbox: Sandbox) -> None:
    result = sandbox.run_populate(codex=False)

    assert result.returncode == 0, result.stderr
    assert sandbox.git("branch", "--show-current", cwd=sandbox.wt).stdout.strip() == ""
    assert (
        sandbox.git(
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/codex/a1b2",
            cwd=sandbox.repo,
            check=False,
        ).returncode
        == 1
    )


def test_existing_branch_at_another_commit_fails_closed(sandbox: Sandbox) -> None:
    (sandbox.repo / "later").write_text("later\n")
    sandbox.git("add", "later", cwd=sandbox.repo)
    sandbox.git("commit", "-q", "-m", "later", cwd=sandbox.repo)
    sandbox.git("branch", "codex/a1b2", cwd=sandbox.repo)

    result = sandbox.run_populate()

    assert result.returncode != 0
    assert "already points at another commit" in result.stderr
    assert sandbox.git("branch", "--show-current", cwd=sandbox.wt).stdout.strip() == ""
