"""Unit tests for test/testrole.py — idempotence regex, argparse, constants."""

import argparse

import pytest
import testrole

# ---------------------------------------------------------------------------
# _count_changed_tasks — ANSI-aware PLAY RECAP parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (["PLAY RECAP *****", "box  : ok=5  changed=3  unreachable=0  failed=0"], 3),
        (["PLAY RECAP *****", "box  : ok=10  changed=0  unreachable=0  failed=0"], 0),
        (["PLAY RECAP *****", "box  : ok=5  \x1b[0;33mchanged=2\x1b[0m  unreachable=0  failed=0"], 2),
        (
            [
                "PLAY RECAP *****",
                "box  : ok=5  changed=1  unreachable=0  failed=0",
                "lab  : ok=3  changed=4  unreachable=0  failed=0",
            ],
            5,
        ),
        (["TASK [debug]", "ok: [box]", ""], 0),
        ([], 0),
        (
            [
                "PLAY RECAP *****",
                "box  : ok=5  changed=1  unreachable=0  failed=0",
                "PLAY RECAP *****",
                "box  : ok=3  changed=2  unreachable=0  failed=0",
            ],
            3,
        ),
        (
            [
                "\x1b[0;32mbox\x1b[0m  : \x1b[0;32mok=10\x1b[0m  "
                "\x1b[0;33mchanged=7\x1b[0m  unreachable=0  "
                "\x1b[0;31mfailed=0\x1b[0m",
            ],
            7,
        ),
    ],
)
def test_count_changed_tasks(stdout: list[str], expected: int) -> None:
    assert testrole._count_changed_tasks(stdout) == expected


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
        with pytest.raises(ValueError, match="invalid literal"):
            testrole._positive_int("abc")


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_ansible_args_still_forward(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["testrole.py", "nginx", "--tags", "homepage"])
        args, pass_args = testrole.parse_args()
        assert args.role == "nginx"
        assert pass_args == ["--tags", "homepage"]
