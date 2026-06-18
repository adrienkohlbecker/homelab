"""Integration tests for mise-tasks/worktree/update.sh (the wt:update task).

update.sh brings a worktree's code branch current with master. notes/ is a
shared gitignored clone the main checkout owns (every worktree symlinks to
it -- see mise-tasks/worktree/populate.sh), not a tracked submodule, so the
update is a plain `git rebase master`: no gitlink to reconcile, and the notes
clone must come through entirely untouched.

These tests build real git fixtures -- a superproject on master with a
gitignored notes clone, a linked worktree with the populate-style notes
symlink -- and run the actual script, asserting:

  * the worktree is clean (`git status --porcelain` empty),
  * the code branch is rebased onto master,
  * the notes symlink and the notes clone's history are untouched,
  * a genuine code conflict halts (set -e) instead of being resolved away.

The script is only validated by shellcheck/shfmt otherwise; this locks in
its behaviour.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATE_SH = REPO_ROOT / "mise-tasks" / "worktree" / "update.sh"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


class Sandbox:
    """A throwaway git universe: a superproject, its notes clone, a worktree.

    All git runs use a hermetic environment (no user/system config, fixed
    identity, no signing) so the suite never touches the operator's real
    config or signing keys.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        home = root / "home"
        home.mkdir()
        self.env = dict(os.environ)
        self.env.update(
            HOME=str(home),
            GIT_CONFIG_GLOBAL="/dev/null",
            GIT_CONFIG_SYSTEM="/dev/null",
            GIT_AUTHOR_NAME="Test",
            GIT_AUTHOR_EMAIL="test@example.com",
            GIT_COMMITTER_NAME="Test",
            GIT_COMMITTER_EMAIL="test@example.com",
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="init.defaultBranch",
            GIT_CONFIG_VALUE_0="master",
        )
        for leak in ("GIT_EDITOR", "EDITOR", "VISUAL", "GIT_DIR", "GIT_WORK_TREE"):
            self.env.pop(leak, None)
        self.repo = root / "repo"

    # -- paths --
    @property
    def repo_notes(self) -> Path:
        return self.repo / "notes"

    @property
    def wt(self) -> Path:
        return self.repo / ".worktrees" / "feat"

    # -- git plumbing --
    def git(self, *args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(["git", *args], cwd=str(cwd), env=self.env, text=True, capture_output=True)
        if check and proc.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} (cwd={cwd}) exited {proc.returncode}\n"
                f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
            )
        return proc

    def rev(self, ref: str, cwd: Path) -> str:
        return self.git("rev-parse", ref, cwd=cwd).stdout.strip()

    def is_ancestor(self, ancestor: str, descendant: str, cwd: Path) -> bool:
        return self.git("merge-base", "--is-ancestor", ancestor, descendant, cwd=cwd, check=False).returncode == 0

    # -- fixture construction --
    def build_repo(self, master_code: bool = True) -> str:
        """Superproject on master with a gitignored notes clone.

        The base commit holds code.txt and the .gitignore that excludes
        notes/; by default master then advances with mcode.txt so there is
        real code to rebase over. Returns the base commit sha.
        """
        self.git("init", "-q", "-b", "master", str(self.repo), cwd=self.root)
        (self.repo / "code.txt").write_text("base\n")
        (self.repo / ".gitignore").write_text("notes\n")
        self.git("add", "code.txt", ".gitignore", cwd=self.repo)
        self.git("commit", "-q", "-m", "base", cwd=self.repo)
        base = self.rev("HEAD", self.repo)
        if master_code:
            (self.repo / "mcode.txt").write_text("m\n")
            self.git("add", "mcode.txt", cwd=self.repo)
            self.git("commit", "-q", "-m", "master: code", cwd=self.repo)
        self.git("init", "-q", str(self.repo_notes), cwd=self.repo)
        (self.repo_notes / "note.md").write_text("a note\n")
        self.git("add", "note.md", cwd=self.repo_notes)
        self.git("commit", "-q", "-m", "N0", cwd=self.repo_notes)
        return base

    def add_worktree(self, base: str) -> None:
        """Linked worktree `feat` off `base` with the populate-style notes symlink."""
        self.git("worktree", "add", "-q", str(self.wt), "-b", "feat", base, cwd=self.repo)
        (self.wt / "notes").symlink_to(self.repo_notes)

    def wt_code_commit(self, fname: str, content: str, msg: str | None = None) -> None:
        (self.wt / fname).write_text(content + "\n")
        self.git("add", fname, cwd=self.wt)
        self.git("commit", "-q", "-m", msg or f"feat: {fname}", cwd=self.wt)

    def repo_code_commit(self, fname: str, content: str, msg: str | None = None) -> None:
        (self.repo / fname).write_text(content + "\n")
        self.git("add", fname, cwd=self.repo)
        self.git("commit", "-q", "-m", msg or f"master: {fname}", cwd=self.repo)

    # -- run the script under test --
    def run_update(self, name: str = "feat") -> subprocess.CompletedProcess[str]:
        env = dict(self.env, usage_worktree=name)
        return subprocess.run(["bash", str(UPDATE_SH)], cwd=str(self.repo), env=env, text=True, capture_output=True)

    # -- invariants --
    def assert_code_rebased(self, branch: str = "feat") -> None:
        assert self.is_ancestor("master", branch, cwd=self.wt), "code branch is not rebased onto master"

    def assert_clean(self) -> None:
        status = self.git("status", "--porcelain", cwd=self.wt).stdout
        assert status == "", f"worktree is not clean after update:\n{status}"

    def assert_notes_untouched(self, notes_head_before: str) -> None:
        notes_link = self.wt / "notes"
        assert notes_link.is_symlink(), "notes symlink is gone"
        assert notes_link.resolve() == self.repo_notes.resolve(), "notes symlink retargeted"
        assert self.rev("HEAD", self.repo_notes) == notes_head_before, "notes clone history moved"


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def test_rebases_code_branch(sandbox: Sandbox) -> None:
    """The core case: own code commits + an advanced master rebase cleanly,
    and the shared notes clone comes through untouched."""
    base = sandbox.build_repo()
    sandbox.add_worktree(base)
    sandbox.wt_code_commit("a.txt", "a")
    sandbox.wt_code_commit("b.txt", "b")
    notes_head = sandbox.rev("HEAD", sandbox.repo_notes)

    result = sandbox.run_update()

    assert result.returncode == 0, result.stderr
    sandbox.assert_clean()
    sandbox.assert_code_rebased()
    sandbox.assert_notes_untouched(notes_head)


def test_update_is_idempotent(sandbox: Sandbox) -> None:
    """A second update with nothing new is a clean no-op (branch sha unchanged)."""
    base = sandbox.build_repo()
    sandbox.add_worktree(base)
    sandbox.wt_code_commit("a.txt", "a")

    assert sandbox.run_update().returncode == 0
    feat_after_first = sandbox.rev("feat", cwd=sandbox.wt)

    second = sandbox.run_update()
    assert second.returncode == 0, second.stderr
    sandbox.assert_clean()
    assert sandbox.rev("feat", cwd=sandbox.wt) == feat_after_first, "second update rewrote an unchanged branch"


def test_noop_when_master_unchanged(sandbox: Sandbox) -> None:
    """master hasn't moved: the rebase is a no-op and nothing is rewritten."""
    base = sandbox.build_repo(master_code=False)
    sandbox.add_worktree(base)
    sandbox.wt_code_commit("a.txt", "a")
    feat_before = sandbox.rev("feat", cwd=sandbox.wt)

    result = sandbox.run_update()

    assert result.returncode == 0, result.stderr
    sandbox.assert_clean()
    assert sandbox.rev("feat", cwd=sandbox.wt) == feat_before


def test_halts_on_code_conflict(sandbox: Sandbox) -> None:
    """A real code conflict must halt the rebase for manual resolution,
    leaving the worktree mid-rebase rather than auto-resolving anything."""
    base = sandbox.build_repo(master_code=False)
    sandbox.repo_code_commit("code.txt", "master", "master: edit code.txt")
    sandbox.add_worktree(base)
    sandbox.wt_code_commit("code.txt", "feat", "feat: edit code.txt")

    result = sandbox.run_update()

    assert result.returncode != 0
    rebase_dir = sandbox.git("rev-parse", "--git-path", "rebase-merge", cwd=sandbox.wt).stdout.strip()
    assert (sandbox.wt / rebase_dir).exists() or Path(rebase_dir).exists(), "rebase did not halt mid-flight"


def test_errors_when_main_worktree_not_on_master(sandbox: Sandbox) -> None:
    base = sandbox.build_repo()
    sandbox.add_worktree(base)
    sandbox.git("checkout", "-q", "-b", "sidebar", cwd=sandbox.repo)

    result = sandbox.run_update()

    assert result.returncode != 0
    assert "expected master" in result.stderr


def test_errors_when_worktree_missing(sandbox: Sandbox) -> None:
    sandbox.build_repo()

    result = sandbox.run_update(name="ghost")

    assert result.returncode != 0
    assert "no worktree found" in result.stderr
