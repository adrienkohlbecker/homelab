"""Unit tests for roles/sort_ini/files/sort_ini.py — INI file sorter."""

import subprocess
import sys
from pathlib import Path

_SORT_INI_PATH = Path(__file__).resolve().parent.parent.parent / "roles" / "sort_ini" / "files" / "sort_ini.py"


class TestSortIni:
    def test_sorts_sections(self, tmp_path: Path) -> None:
        ini = tmp_path / "test.ini"
        ini.write_text("[zebra]\nz_key = 1\n[alpha]\na_key = 2\n")
        subprocess.run([sys.executable, str(_SORT_INI_PATH), str(ini)], check=True, timeout=30)
        result = ini.read_text()
        assert result.index("[alpha]") < result.index("[zebra]")

    def test_sorts_keys_within_section(self, tmp_path: Path) -> None:
        ini = tmp_path / "test.ini"
        ini.write_text("[section]\nz_key = 1\na_key = 2\nm_key = 3\n")
        subprocess.run([sys.executable, str(_SORT_INI_PATH), str(ini)], check=True, timeout=30)
        result = ini.read_text()
        assert result.index("a_key") < result.index("m_key") < result.index("z_key")

    def test_handles_subsections(self, tmp_path: Path) -> None:
        ini = tmp_path / "test.ini"
        ini.write_text("[main]\n[[sub_b]]\nb = 1\n[[sub_a]]\na = 2\n")
        subprocess.run([sys.executable, str(_SORT_INI_PATH), str(ini)], check=True, timeout=30)
        result = ini.read_text()
        assert result.index("[[sub_a]]") < result.index("[[sub_b]]")

    def test_idempotent(self, tmp_path: Path) -> None:
        ini = tmp_path / "test.ini"
        ini.write_text("[b]\nz = 1\na = 2\n[a]\nx = 3\n")
        subprocess.run([sys.executable, str(_SORT_INI_PATH), str(ini)], check=True, timeout=30)
        first = ini.read_text()
        subprocess.run([sys.executable, str(_SORT_INI_PATH), str(ini)], check=True, timeout=30)
        second = ini.read_text()
        assert first == second

    def test_empty_file(self, tmp_path: Path) -> None:
        ini = tmp_path / "test.ini"
        ini.write_text("")
        subprocess.run([sys.executable, str(_SORT_INI_PATH), str(ini)], check=True, timeout=30)
        assert ini.read_text() == ""

    def test_preserves_values(self, tmp_path: Path) -> None:
        ini = tmp_path / "test.ini"
        ini.write_text("[section]\nkey = value with spaces\nother = 123\n")
        subprocess.run([sys.executable, str(_SORT_INI_PATH), str(ini)], check=True, timeout=30)
        result = ini.read_text()
        assert "key = value with spaces" in result
        assert "other = 123" in result

    def test_usage_on_no_args(self) -> None:
        result = subprocess.run(
            [sys.executable, str(_SORT_INI_PATH)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert "USAGE" in result.stdout
