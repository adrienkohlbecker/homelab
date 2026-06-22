"""Unit tests for custom ansible-lint rules."""

import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_RULES = runpy.run_path(str(_ROOT / "lint" / "ansible_rules" / "homelab.py"))
RequireBackup = _RULES["RequireBackup"]
ShellStrictMode = _RULES["ShellStrictMode"]


def _task(module: str, **action):
    return {"__ansible_action_type__": "task", "action": {"__ansible_module__": module, **action}}


def _lintable(path: str):
    return SimpleNamespace(path=Path(path))


class TestShellStrictMode:
    @pytest.mark.parametrize(
        ("cmd", "missing"),
        [
            ("set -euo pipefail\necho hello", []),
            ("set -e\nset -u\nset -o pipefail\necho hello", []),
            ("set -uo pipefail\necho hello", ["set -e"]),
            ("set -eo pipefail\necho hello", ["set -u"]),
            ("set -eu\necho hello", ["set -o pipefail"]),
            ("echo hello", ["set -e", "set -u", "set -o pipefail"]),
            ("unset -e FOO", ["set -e", "set -u", "set -o pipefail"]),
            ("reset-property -e something", ["set -e", "set -u", "set -o pipefail"]),
            ("set -euo pipefail; echo hello", []),
        ],
    )
    def test_strict_mode_requirements(self, cmd: str, missing: list[str]) -> None:
        result = ShellStrictMode().matchtask(_task("shell", _raw_params=cmd, executable="/bin/bash"))
        expected = False if not missing else f"shell task missing {', '.join(missing)}"
        assert result == expected

    def test_requires_bash(self) -> None:
        result = ShellStrictMode().matchtask(
            _task("shell", _raw_params="set -euo pipefail\necho hello", executable="/bin/sh")
        )
        assert result == "shell task missing executable: /bin/bash"

    def test_test_hooks_are_exempt(self) -> None:
        result = ShellStrictMode().matchtask(
            _task("shell", _raw_params="echo hello"), _lintable("roles/x/tasks/_verify.yml")
        )
        assert result is False


class TestRequireBackup:
    @pytest.mark.parametrize(
        "module",
        ["copy", "template", "replace", "lineinfile", "blockinfile", "assemble", "ini_file"],
    )
    def test_requires_backup(self, module: str) -> None:
        assert RequireBackup().matchtask(_task(module)) == f"{module} task is missing `backup: true`"

    def test_accepts_fqcn_and_templated_backup(self) -> None:
        assert RequireBackup().matchtask(_task("community.general.ini_file", backup="{{ keep_backup }}")) is False

    def test_flags_backup_false(self) -> None:
        assert (
            RequireBackup().matchtask(_task("copy", backup=False))
            == "copy task sets `backup: false`; config writes must keep backups"
        )

    def test_ignores_non_file_write_modules(self) -> None:
        assert RequireBackup().matchtask(_task("file")) is False

    @pytest.mark.parametrize(
        "path",
        ["roles/x/tasks/_setup.yml", "roles/test/tasks/main.yml", "roles/packer/tasks/main.yml"],
    )
    def test_test_files_are_exempt(self, path: str) -> None:
        assert RequireBackup().matchtask(_task("copy"), _lintable(path)) is False


class TestTestMetaValidation:
    def test_all_meta_files_valid(self) -> None:
        """Run test-meta.py against the real repo — catches typos in machine/ubuntu."""
        result = subprocess.run(
            [
                sys.executable,
                str(_ROOT / "mise-tasks" / "lint" / "test-meta.py"),
            ],
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
            timeout=30,
        )
        assert result.returncode == 0, f"test-meta.py failed:\n{result.stderr}"
        assert "Validated" in result.stdout
