"""Unit tests for SABnzbd's INI canonicalizer."""

import subprocess
import sys
from pathlib import Path

import pytest

_SORT_INI_PATH = Path(__file__).resolve().parent.parent / "roles" / "sabnzbd" / "files" / "sort_ini.py"


def _run_sort(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SORT_INI_PATH), str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestSortIni:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            (
                "[zebra]\nz_key = 1\n[alpha]\na_key = 2\n",
                "[alpha]\na_key = 2\n[zebra]\nz_key = 1\n",
            ),
            (
                "[section]\nz_key = 1\na_key = 2\nm_key = 3\n",
                "[section]\na_key = 2\nm_key = 3\nz_key = 1\n",
            ),
            (
                "[main]\n[[sub_b]]\nb = 1\n[[sub_a]]\na = 2\n",
                "[main]\n[[sub_a]]\na = 2\n[[sub_b]]\nb = 1\n",
            ),
            ("", ""),
            (
                "[section]\nother = 123\nkey = value with spaces\n",
                "[section]\nkey = value with spaces\nother = 123\n",
            ),
            (
                "[section]\nz_key = 1\n\na_key = 2\n",
                "[section]\na_key = 2\nz_key = 1\n",
            ),
        ],
    )
    def test_canonicalizes(self, tmp_path: Path, source: str, expected: str) -> None:
        ini = tmp_path / "test.ini"
        ini.write_text(source)

        _run_sort(ini)

        assert ini.read_text() == expected

    def test_idempotent(self, tmp_path: Path) -> None:
        ini = tmp_path / "test.ini"
        ini.write_text("[b]\nz = 1\na = 2\n[a]\nx = 3\n")
        first_run = _run_sort(ini)
        first = ini.read_text()
        second_run = _run_sort(ini)
        second = ini.read_text()
        assert "canonicalized" in first_run.stdout
        assert second_run.stdout == ""
        assert first == second

    def test_usage_on_no_args(self) -> None:
        result = subprocess.run(
            [sys.executable, str(_SORT_INI_PATH)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert "USAGE" in result.stderr
        assert result.returncode == 1

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [sys.executable, str(_SORT_INI_PATH), str(tmp_path / "does_not_exist.ini")],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "not found" in result.stderr
