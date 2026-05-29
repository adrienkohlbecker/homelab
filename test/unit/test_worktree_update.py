"""Integration tests for mise-tasks/worktree/update.sh (the wt:update task).

update.sh rebases a worktree's code branch onto master AND rebases its notes
submodule onto the commit master records for notes (master:notes), keeping the
two histories consistent. The hard case is when the worktree carries its own
notes commits while master:notes has independently advanced: the two notes lines
DIVERGE, and a plain `git rebase` halts on a submodule-gitlink conflict at every
gitlink-bump commit. update.sh rebases notes first (so the rewritten own-commit
SHAs become descendants of master:notes), then drives the code rebase,
remapping each notes-gitlink conflict to the rebased SHA.

These tests build real git fixtures -- a notes "origin", a superproject on
master, a linked worktree with its own notes clone (mirroring how worktree:add +
worktree:populate set things up) -- and run the actual script, asserting the
post-conditions that define a consistent dual history:

  * the worktree is clean (`git status --porcelain` empty),
  * the code branch is rebased onto master,
  * every code commit's recorded notes gitlink is a commit on the rebased notes
    branch (no orphaned/dangling pointers),
  * HEAD:notes equals the notes branch tip,
  * the notes branch is rebased onto master:notes.

The script is only validated by shellcheck/shfmt otherwise; this locks in its
behaviour, including the bugs that were easy to introduce (a `set -e` trap on
the no-own-commits path, the divergent-notes conflict resolution).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_SH = REPO_ROOT / "mise-tasks" / "worktree" / "update.sh"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


class Sandbox:
    """A throwaway git universe: a notes origin, a superproject, and a worktree.

    All git runs use a hermetic environment (no user/system config, fixed
    identity, no signing, file-protocol submodules allowed) so the suite never
    touches the operator's real config or signing keys.
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
            GIT_CONFIG_COUNT="2",
            GIT_CONFIG_KEY_0="protocol.file.allow",
            GIT_CONFIG_VALUE_0="always",
            GIT_CONFIG_KEY_1="init.defaultBranch",
            GIT_CONFIG_VALUE_1="master",
        )
        for leak in ("GIT_EDITOR", "EDITOR", "VISUAL", "GIT_DIR", "GIT_WORK_TREE"):
            self.env.pop(leak, None)
        self.notes_origin = root / "notes_origin"
        self.repo = root / "repo"

    # -- paths --
    @property
    def repo_notes(self) -> Path:
        return self.repo / "notes"

    @property
    def wt(self) -> Path:
        return self.repo / ".worktrees" / "feat"

    @property
    def wt_notes(self) -> Path:
        return self.wt / "notes"

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
    def build_notes_origin(self, master_notes_extra: int) -> tuple[str, str]:
        """notes 'origin' on `main`: B0 then `master_notes_extra` further commits.

        Returns (B0, tip). tip is what the superproject will record as
        master:notes; B0 is the older base the worktree forks its notes from.
        """
        self.git("init", "-q", "-b", "main", str(self.notes_origin), cwd=self.root)
        self.git("commit", "-q", "--allow-empty", "-m", "B0", cwd=self.notes_origin)
        b0 = self.rev("HEAD", self.notes_origin)
        for i in range(master_notes_extra):
            self.git("commit", "-q", "--allow-empty", "-m", f"M{i + 1}", cwd=self.notes_origin)
        return b0, self.rev("HEAD", self.notes_origin)

    def build_repo_with_notes(self, base_notes: str, master_notes: str, master_code: bool = True) -> str:
        """Superproject on master with a `notes` submodule.

        Base commit records notes=base_notes and a code.txt; the master commit
        advances the gitlink to master_notes (and, by default, adds mcode.txt so
        there is real code to rebase over). Returns the base commit sha.
        """
        self.git("init", "-q", "-b", "master", str(self.repo), cwd=self.root)
        self.git("submodule", "add", "-q", str(self.notes_origin), "notes", cwd=self.repo)
        self.git("checkout", "-q", base_notes, cwd=self.repo_notes)
        (self.repo / "code.txt").write_text("base\n")
        self.git("add", ".gitmodules", "notes", "code.txt", cwd=self.repo)
        self.git("commit", "-q", "-m", "base: notes=B0", cwd=self.repo)
        base = self.rev("HEAD", self.repo)
        self.git("checkout", "-q", master_notes, cwd=self.repo_notes)
        self.git("add", "notes", cwd=self.repo)
        if master_code:
            (self.repo / "mcode.txt").write_text("m\n")
            self.git("add", "mcode.txt", cwd=self.repo)
        self.git("commit", "-q", "-m", "master: advance notes + code", cwd=self.repo)
        return base

    def build_repo_plain(self) -> str:
        """Superproject on master with NO notes submodule. Returns base sha."""
        self.git("init", "-q", "-b", "master", str(self.repo), cwd=self.root)
        (self.repo / "code.txt").write_text("base\n")
        self.git("add", "code.txt", cwd=self.repo)
        self.git("commit", "-q", "-m", "base", cwd=self.repo)
        base = self.rev("HEAD", self.repo)
        (self.repo / "mcode.txt").write_text("m\n")
        self.git("add", "mcode.txt", cwd=self.repo)
        self.git("commit", "-q", "-m", "master: code", cwd=self.repo)
        return base

    def add_worktree(self, base: str, *, has_submodule: bool = True, notes_branch: bool = True) -> None:
        """Linked worktree `feat` off `base`, with its own notes clone.

        notes_branch=False leaves the notes clone detached at the recorded
        gitlink (the agent/isolation shape worktree:populate produces when
        new==base).
        """
        self.git("worktree", "add", "-q", str(self.wt), "-b", "feat", base, cwd=self.repo)
        if has_submodule:
            self.git("submodule", "update", "--init", "-q", "notes", cwd=self.wt)
            if notes_branch:
                self.git("checkout", "-q", "-b", "feat", cwd=self.wt_notes)

    def wt_own_notes_commit(self, label: str) -> str:
        """Add a notes commit on the worktree's notes branch + bump the gitlink."""
        self.git("commit", "-q", "--allow-empty", "-m", label, cwd=self.wt_notes)
        sha = self.rev("HEAD", self.wt_notes)
        self.git("add", "notes", cwd=self.wt)
        self.git("commit", "-q", "-m", f"feat: notes={label}", cwd=self.wt)
        return sha

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
        env = dict(self.env, usage_name=name)
        return subprocess.run(["bash", str(UPDATE_SH)], cwd=str(self.repo), env=env, text=True, capture_output=True)

    # -- invariants --
    def assert_code_rebased(self, branch: str = "feat") -> None:
        assert self.is_ancestor("master", branch, cwd=self.wt), "code branch is not rebased onto master"

    def assert_clean(self) -> None:
        status = self.git("status", "--porcelain", cwd=self.wt).stdout
        assert status == "", f"worktree is not clean after update:\n{status}"

    def assert_notes_consistent(self, branch: str = "feat") -> None:
        master_notes = self.rev("master:notes", cwd=self.repo)
        notes_tip = self.rev(branch, cwd=self.wt_notes)
        assert self.rev(f"{branch}:notes", cwd=self.wt) == notes_tip, "HEAD:notes does not equal the notes branch tip"
        assert self.is_ancestor(
            master_notes, branch, cwd=self.wt_notes
        ), "notes branch is not rebased onto master:notes"
        for commit in self.git("rev-list", f"master..{branch}", cwd=self.wt).stdout.split():
            gitlink = self.rev(f"{commit}:notes", cwd=self.wt)
            assert self.is_ancestor(
                gitlink, branch, cwd=self.wt_notes
            ), f"commit {commit} pins notes {gitlink}, which is not on the rebased notes branch"

    def assert_fully_consistent(self, branch: str = "feat") -> None:
        self.assert_clean()
        self.assert_code_rebased(branch)
        self.assert_notes_consistent(branch)


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def test_divergent_notes_stays_consistent(sandbox: Sandbox) -> None:
    """The core case: own notes commits + an independently-advanced master:notes.

    A plain rebase would halt on a submodule conflict at each gitlink bump;
    update.sh must remap them and finish with a clean, consistent tree.
    """
    b0, master_notes = sandbox.build_notes_origin(master_notes_extra=3)
    base = sandbox.build_repo_with_notes(b0, master_notes)
    sandbox.add_worktree(base)
    sandbox.wt_code_commit("fcode.txt", "f0", "feat: code only (no notes)")
    sandbox.wt_own_notes_commit("F1")
    sandbox.wt_own_notes_commit("F2")

    result = sandbox.run_update()

    assert result.returncode == 0, result.stderr
    sandbox.assert_fully_consistent()


def test_divergent_update_is_idempotent(sandbox: Sandbox) -> None:
    """A second update with nothing new is a clean no-op (branch sha unchanged)."""
    b0, master_notes = sandbox.build_notes_origin(master_notes_extra=2)
    base = sandbox.build_repo_with_notes(b0, master_notes)
    sandbox.add_worktree(base)
    sandbox.wt_own_notes_commit("F1")
    sandbox.wt_code_commit("fcode.txt", "f", "feat: code")
    sandbox.wt_own_notes_commit("F2")

    assert sandbox.run_update().returncode == 0
    sandbox.assert_fully_consistent()
    feat_after_first = sandbox.rev("feat", cwd=sandbox.wt)

    second = sandbox.run_update()
    assert second.returncode == 0, second.stderr
    sandbox.assert_fully_consistent()
    assert sandbox.rev("feat", cwd=sandbox.wt) == feat_after_first, "second update rewrote an unchanged branch"


def test_common_no_own_notes_commits(sandbox: Sandbox) -> None:
    """Worktree has only code commits; master advanced both code and notes.

    The notes branch fast-forwards to master:notes and the code rebase needs no
    conflict resolution. This path also tripped a `set -e` bug (empty map input).
    """
    b0, master_notes = sandbox.build_notes_origin(master_notes_extra=3)
    base = sandbox.build_repo_with_notes(b0, master_notes)
    sandbox.add_worktree(base)
    sandbox.wt_code_commit("a.txt", "a")
    sandbox.wt_code_commit("b.txt", "b")

    result = sandbox.run_update()

    assert result.returncode == 0, result.stderr
    sandbox.assert_fully_consistent()
    assert sandbox.rev("feat:notes", cwd=sandbox.wt) == master_notes


def test_own_notes_but_master_notes_unchanged(sandbox: Sandbox) -> None:
    """Own notes commits, but master only advanced code (master:notes == base).

    The notes rebase is a no-op (identity map), and the code gitlinks already
    point at descendants of master:notes, so the rebase fast-forward-resolves
    without ever consulting the map.
    """
    b0, _ = sandbox.build_notes_origin(master_notes_extra=0)
    base = sandbox.build_repo_with_notes(base_notes=b0, master_notes=b0, master_code=True)
    sandbox.add_worktree(base)
    sandbox.wt_code_commit("fcode.txt", "f", "feat: code")
    sandbox.wt_own_notes_commit("F1")
    sandbox.wt_own_notes_commit("F2")

    result = sandbox.run_update()

    assert result.returncode == 0, result.stderr
    sandbox.assert_fully_consistent()


def test_no_notes_submodule_rebases_code(sandbox: Sandbox) -> None:
    """A repo with no notes submodule: update just rebases the code branch.

    Exercises the master:notes-empty branch and the final cleanup that must not
    fail when no map directory was created (a second `set -e` trap).
    """
    base = sandbox.build_repo_plain()
    sandbox.add_worktree(base, has_submodule=False)
    sandbox.wt_code_commit("fcode.txt", "f", "feat: code")

    result = sandbox.run_update()

    assert result.returncode == 0, result.stderr
    sandbox.assert_clean()
    sandbox.assert_code_rebased()


def test_detached_notes_worktree_skips_notes_step(sandbox: Sandbox) -> None:
    """A detached notes clone (no `feat` notes branch) skips the notes rebase.

    Mirrors an agent/isolation worktree (new==base). master:notes is left at the
    base value so the gitlink needs no movement and the tree stays clean.
    """
    b0, _ = sandbox.build_notes_origin(master_notes_extra=0)
    base = sandbox.build_repo_with_notes(base_notes=b0, master_notes=b0, master_code=True)
    sandbox.add_worktree(base, notes_branch=False)
    sandbox.wt_code_commit("fcode.txt", "f", "feat: code")

    result = sandbox.run_update()

    assert result.returncode == 0, result.stderr
    sandbox.assert_clean()
    sandbox.assert_code_rebased()
    assert sandbox.rev("feat:notes", cwd=sandbox.wt) == b0


def test_halts_on_non_notes_code_conflict(sandbox: Sandbox) -> None:
    """A real code conflict must halt the rebase, not be auto-resolved away."""
    b0, master_notes = sandbox.build_notes_origin(master_notes_extra=2)
    base = sandbox.build_repo_with_notes(b0, master_notes, master_code=False)
    sandbox.repo_code_commit("code.txt", "master", "master: edit code.txt")
    sandbox.add_worktree(base)
    sandbox.wt_code_commit("code.txt", "feat", "feat: edit code.txt")

    result = sandbox.run_update()

    assert result.returncode == 1
    assert "beyond the notes gitlink" in result.stderr


def test_errors_when_main_worktree_not_on_master(sandbox: Sandbox) -> None:
    b0, master_notes = sandbox.build_notes_origin(master_notes_extra=1)
    base = sandbox.build_repo_with_notes(b0, master_notes)
    sandbox.add_worktree(base)
    sandbox.git("checkout", "-q", "-b", "sidebar", cwd=sandbox.repo)

    result = sandbox.run_update()

    assert result.returncode != 0
    assert "expected master" in result.stderr


def test_errors_when_worktree_missing(sandbox: Sandbox) -> None:
    b0, master_notes = sandbox.build_notes_origin(master_notes_extra=1)
    sandbox.build_repo_with_notes(b0, master_notes)

    result = sandbox.run_update(name="ghost")

    assert result.returncode != 0
    assert "not found" in result.stderr
