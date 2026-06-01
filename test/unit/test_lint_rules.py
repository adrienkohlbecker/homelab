"""Unit tests for custom ansible-lint rules.

Tests the regex matching and task-classification logic in
lint/ansible_rules/{require_backup,shell_strict_mode}.py without needing
a full ansible-lint invocation.
"""

import re
import subprocess
import sys
from pathlib import Path

_LINT_DIR = Path(__file__).resolve().parent.parent.parent / "lint" / "ansible_rules"


# ---------------------------------------------------------------------------
# shell_strict_mode regexes (extracted to avoid importing ansiblelint)
# ---------------------------------------------------------------------------


def _load_regexes():
    set_e = re.compile(r"(?<![\w-])set(?![\w-])[^\n;]*-[A-Za-z]*e", re.MULTILINE)
    set_u = re.compile(r"(?<![\w-])set(?![\w-])[^\n;]*-[A-Za-z]*u", re.MULTILINE)
    pipefail = re.compile(r"(?<![\w-])set(?![\w-])[^\n]*pipefail", re.MULTILINE)
    return set_e, set_u, pipefail


SET_E_RE, SET_U_RE, PIPEFAIL_RE = _load_regexes()


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


_FILE_WRITE_MODULES = frozenset({
    "copy", "template", "replace", "lineinfile",
    "blockinfile", "assemble", "ini_file",
})


class TestRequireBackupModuleSet:
    def test_copy_is_covered(self) -> None:
        assert "copy" in _FILE_WRITE_MODULES

    def test_template_is_covered(self) -> None:
        assert "template" in _FILE_WRITE_MODULES

    def test_replace_is_covered(self) -> None:
        assert "replace" in _FILE_WRITE_MODULES

    def test_lineinfile_is_covered(self) -> None:
        assert "lineinfile" in _FILE_WRITE_MODULES

    def test_blockinfile_is_covered(self) -> None:
        assert "blockinfile" in _FILE_WRITE_MODULES

    def test_ini_file_is_covered(self) -> None:
        assert "ini_file" in _FILE_WRITE_MODULES

    def test_file_module_excluded(self) -> None:
        assert "file" not in _FILE_WRITE_MODULES

    def test_command_excluded(self) -> None:
        assert "command" not in _FILE_WRITE_MODULES

    def test_matches_source(self) -> None:
        text = (_LINT_DIR / "require_backup.py").read_text()
        for mod in _FILE_WRITE_MODULES:
            assert f'"{mod}"' in text


# ---------------------------------------------------------------------------
# test-meta.py validation (against real repo)
# ---------------------------------------------------------------------------


class TestTestMetaValidation:
    def test_all_meta_files_valid(self) -> None:
        """Run test-meta.py against the real repo — catches typos in machine/ubuntu."""
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent.parent / "mise-tasks" / "lint" / "test-meta.py")],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent.parent),
        )
        assert result.returncode == 0, f"test-meta.py failed:\n{result.stderr}"
        assert "Validated" in result.stdout
