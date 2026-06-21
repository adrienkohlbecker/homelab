"""Unit tests for mise-tasks/ci/role-deps.py — inverse-dependency lookup."""

import subprocess
import sys
from pathlib import Path

_ROLE_DEPS_PATH = Path(__file__).resolve().parent.parent / "mise-tasks" / "ci" / "role-deps.py"


class TestCli:
    def test_finds_consumers(self, tmp_path: Path) -> None:
        roles = tmp_path / "roles"
        for name, content in [
            ("nginx", "---\n"),
            ("pihole", "- import_role:\n    name: nginx\n"),
            ("netdata", "- import_role:\n    name: nginx\n"),
            ("standalone", "- command: echo hi\n"),
        ]:
            task_dir = roles / name / "tasks"
            task_dir.mkdir(parents=True)
            (task_dir / "main.yml").write_text(content)

        result = subprocess.run(
            [sys.executable, str(_ROLE_DEPS_PATH), "nginx"],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            timeout=30,
        )
        assert result.returncode == 0
        consumers = result.stdout.strip().splitlines()
        assert sorted(consumers) == ["netdata", "pihole"]

    def test_no_consumers_empty_output(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "roles" / "leaf" / "tasks"
        task_dir.mkdir(parents=True)
        (task_dir / "main.yml").write_text("- command: echo hi\n")

        result = subprocess.run(
            [sys.executable, str(_ROLE_DEPS_PATH), "leaf"],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            timeout=30,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_usage_on_missing_arg(self) -> None:
        result = subprocess.run(
            [sys.executable, str(_ROLE_DEPS_PATH)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 2

    def test_block_nesting_via_cli(self, tmp_path: Path) -> None:
        roles = tmp_path / "roles"
        task_dir = roles / "homepage" / "tasks"
        task_dir.mkdir(parents=True)
        (task_dir / "main.yml").write_text("- block:\n  - import_role:\n      name: nginx\n")
        task_dir2 = roles / "nginx" / "tasks"
        task_dir2.mkdir(parents=True)
        (task_dir2 / "main.yml").write_text("---\n")

        result = subprocess.run(
            [sys.executable, str(_ROLE_DEPS_PATH), "nginx"],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            timeout=30,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "homepage"
