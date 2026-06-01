"""Unit tests for test/testrole.py — idempotence regex, argparse type, constants."""

import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import testrole


# ---------------------------------------------------------------------------
# _count_changed_tasks — ANSI-aware PLAY RECAP parser
# ---------------------------------------------------------------------------


class TestCountChangedTasks:
    def test_plain_recap_single_host(self) -> None:
        stdout = ["PLAY RECAP *****", "box  : ok=5  changed=3  unreachable=0  failed=0"]
        assert testrole._count_changed_tasks(stdout) == 3

    def test_plain_recap_zero_changed(self) -> None:
        stdout = ["PLAY RECAP *****", "box  : ok=10  changed=0  unreachable=0  failed=0"]
        assert testrole._count_changed_tasks(stdout) == 0

    def test_ansi_colored_recap(self) -> None:
        stdout = [
            "PLAY RECAP *****",
            "box  : ok=5  \x1b[0;33mchanged=2\x1b[0m  unreachable=0  failed=0",
        ]
        assert testrole._count_changed_tasks(stdout) == 2

    def test_multiple_hosts(self) -> None:
        stdout = [
            "PLAY RECAP *****",
            "box  : ok=5  changed=1  unreachable=0  failed=0",
            "lab  : ok=3  changed=4  unreachable=0  failed=0",
        ]
        assert testrole._count_changed_tasks(stdout) == 5

    def test_no_recap_lines(self) -> None:
        stdout = ["TASK [debug]", "ok: [box]", ""]
        assert testrole._count_changed_tasks(stdout) == 0

    def test_empty_stdout(self) -> None:
        assert testrole._count_changed_tasks([]) == 0

    def test_multiple_recaps(self) -> None:
        stdout = [
            "PLAY RECAP *****",
            "box  : ok=5  changed=1  unreachable=0  failed=0",
            "PLAY RECAP *****",
            "box  : ok=3  changed=2  unreachable=0  failed=0",
        ]
        assert testrole._count_changed_tasks(stdout) == 3

    def test_heavy_ansi_wrapping(self) -> None:
        stdout = [
            "\x1b[0;32mbox\x1b[0m  : \x1b[0;32mok=10\x1b[0m  "
            "\x1b[0;33mchanged=7\x1b[0m  unreachable=0  "
            "\x1b[0;31mfailed=0\x1b[0m",
        ]
        assert testrole._count_changed_tasks(stdout) == 7


# ---------------------------------------------------------------------------
# _ANSI_CSI_RE — escape stripping
# ---------------------------------------------------------------------------


class TestAnsiCsiRe:
    def test_strips_color_codes(self) -> None:
        line = "\x1b[0;33mchanged=2\x1b[0m"
        assert testrole._ANSI_CSI_RE.sub("", line) == "changed=2"

    def test_strips_multi_param_codes(self) -> None:
        line = "\x1b[38;5;196mred text\x1b[0m"
        assert testrole._ANSI_CSI_RE.sub("", line) == "red text"

    def test_passthrough_no_escapes(self) -> None:
        line = "plain text"
        assert testrole._ANSI_CSI_RE.sub("", line) == "plain text"


# ---------------------------------------------------------------------------
# _positive_int — argparse type
# ---------------------------------------------------------------------------


class TestPositiveInt:
    def test_valid_positive(self) -> None:
        assert testrole._positive_int("42") == 42

    def test_one_is_valid(self) -> None:
        assert testrole._positive_int("1") == 1

    def test_zero_raises(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError, match="positive integer"):
            testrole._positive_int("0")

    def test_negative_raises(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError, match="positive integer"):
            testrole._positive_int("-5")

    def test_non_numeric_raises(self) -> None:
        with pytest.raises(ValueError):
            testrole._positive_int("abc")


# ---------------------------------------------------------------------------
# _SKIP_MIRRORS_PRELUDE_ROLES
# ---------------------------------------------------------------------------


class TestSkipMirrorsPreludeRoles:
    def test_apt_is_skipped(self) -> None:
        assert "apt" in testrole._SKIP_MIRRORS_PRELUDE_ROLES

    def test_packer_is_skipped(self) -> None:
        assert "packer" in testrole._SKIP_MIRRORS_PRELUDE_ROLES

    def test_nginx_not_skipped(self) -> None:
        assert "nginx" not in testrole._SKIP_MIRRORS_PRELUDE_ROLES
