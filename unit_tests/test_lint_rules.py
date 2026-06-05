"""Unit tests for custom ansible-lint rules.

Tests the regex matching and task-classification logic in
lint/ansible_rules/{require_backup,shell_strict_mode}.py without needing
a full ansible-lint invocation.
"""

import importlib
import subprocess
import sys
from pathlib import Path

_LINT_DIR = Path(__file__).resolve().parent.parent / "lint" / "ansible_rules"


# ---------------------------------------------------------------------------
# shell_strict_mode regexes — imported from the real rule module
# ---------------------------------------------------------------------------


def _load_shell_strict_mode():
    spec = importlib.util.spec_from_file_location("shell_strict_mode", _LINT_DIR / "shell_strict_mode.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ssm = _load_shell_strict_mode()
SET_E_RE, SET_U_RE, PIPEFAIL_RE = _ssm._SET_E_RE, _ssm._SET_U_RE, _ssm._PIPEFAIL_RE


class TestShellStrictModeRegex:
    def test_combined_set(self) -> None:
        cmd = "set -euo pipefail\necho hello"
        assert SET_E_RE.search(cmd)
        assert SET_U_RE.search(cmd)
        assert PIPEFAIL_RE.search(cmd)

    def test_split_set_statements(self) -> None:
        cmd = "set -e\nset -u\nset -o pipefail\necho hello"
        assert SET_E_RE.search(cmd)
        assert SET_U_RE.search(cmd)
        assert PIPEFAIL_RE.search(cmd)

    def test_missing_e(self) -> None:
        cmd = "set -uo pipefail\necho hello"
        assert not SET_E_RE.search(cmd)
        assert SET_U_RE.search(cmd)

    def test_missing_u(self) -> None:
        cmd = "set -eo pipefail\necho hello"
        assert SET_E_RE.search(cmd)
        assert not SET_U_RE.search(cmd)

    def test_missing_pipefail(self) -> None:
        cmd = "set -eu\necho hello"
        assert SET_E_RE.search(cmd)
        assert SET_U_RE.search(cmd)
        assert not PIPEFAIL_RE.search(cmd)

    def test_no_set_at_all(self) -> None:
        cmd = "echo hello"
        assert not SET_E_RE.search(cmd)
        assert not SET_U_RE.search(cmd)
        assert not PIPEFAIL_RE.search(cmd)

    def test_unset_does_not_match(self) -> None:
        cmd = "unset -e FOO"
        assert not SET_E_RE.search(cmd)

    def test_reset_does_not_match(self) -> None:
        cmd = "reset-property -e something"
        assert not SET_E_RE.search(cmd)

    def test_semicolon_separated(self) -> None:
        cmd = "set -euo pipefail; echo hello"
        assert SET_E_RE.search(cmd)
        assert SET_U_RE.search(cmd)
        assert PIPEFAIL_RE.search(cmd)


# ---------------------------------------------------------------------------
# require_backup — module classification
# ---------------------------------------------------------------------------


class TestRequireBackupModuleSet:
    def test_expected_modules_in_source(self) -> None:
        text = (_LINT_DIR / "require_backup.py").read_text()
        for mod in ("copy", "template", "replace", "lineinfile", "blockinfile", "assemble", "ini_file"):
            assert f'"{mod}"' in text


# ---------------------------------------------------------------------------
# test-meta.py validation (against real repo)
# ---------------------------------------------------------------------------


class TestTestMetaValidation:
    def test_all_meta_files_valid(self) -> None:
        """Run test-meta.py against the real repo — catches typos in machine/ubuntu."""
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parent.parent / "mise-tasks" / "lint" / "test-meta.py"),
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
            timeout=30,
        )
        assert result.returncode == 0, f"test-meta.py failed:\n{result.stderr}"
        assert "Validated" in result.stdout
