import gzip
import importlib.util
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "roles/lnav/files/lnav_journal.py"
SPEC = importlib.util.spec_from_file_location("lnav_journal", MODULE_PATH)
assert SPEC
assert SPEC.loader
lnav_journal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lnav_journal)


def write_record(path: Path, timestamp: str, *, compressed: bool = False) -> None:
    content = f'{{"time":"{timestamp}"}}\n'
    if compressed:
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            stream.write(content)
    else:
        path.write_text(content)


def test_parse_args_separates_wrapper_options() -> None:
    now = datetime(2026, 7, 12, 12, tzinfo=UTC)
    parsed = lnav_journal.parse_args(["-S", "2 hours ago", "-u", "ssh", "--all", "-n"], now)

    assert parsed == ("2 hours ago", None, True, ["ssh"], ["-S", "2026-07-12T10:00:00+00:00", "-n"])


def test_parse_args_normalizes_equals_bounds_against_one_clock() -> None:
    now = datetime(2026, 7, 12, 12, 34, 56, tzinfo=UTC)

    parsed = lnav_journal.parse_args(["--since=10 days ago", "--until=now"], now)

    assert parsed == (
        "10 days ago",
        "now",
        False,
        [],
        ["--since=2026-07-02T12:34:56+00:00", "--until=2026-07-12T12:34:56+00:00"],
    )


def test_parse_args_preserves_unknown_lnav_time_syntax() -> None:
    parsed = lnav_journal.parse_args(["-S", "last blue moon"], datetime(2026, 7, 12, tzinfo=UTC))

    assert parsed[-1] == ["-S", "last blue moon"]


def test_line_count_mode_is_rejected() -> None:
    with pytest.raises(SystemExit):
        lnav_journal.parse_args(["-n", "100"])


def test_unit_filter_normalizes_names_and_preserves_globs() -> None:
    expression = lnav_journal.unit_filter(["homeassistant", "session-*.scope", "odd'name.service"])

    assert expression == (
        ":unit = 'homeassistant.service' OR " ":unit GLOB 'session-*.scope' OR " ":unit = 'odd''name.service'"
    )


def test_discover_logs_sorts_generations_numerically(tmp_path: Path) -> None:
    for name in ("lnav.jsonl", "lnav.jsonl.10", "lnav.jsonl.2.gz", "lnav.jsonl.unrelated"):
        (tmp_path / name).touch()

    assert [path.name for path in lnav_journal.discover_logs(tmp_path / "lnav.jsonl")] == [
        "lnav.jsonl",
        "lnav.jsonl.2.gz",
        "lnav.jsonl.10",
    ]


def test_select_logs_uses_time_bounds_for_plain_and_gzip_files(tmp_path: Path) -> None:
    current = tmp_path / "lnav.jsonl"
    previous = tmp_path / "lnav.jsonl.1.gz"
    write_record(current, "2026-07-12T00:00:00Z")
    write_record(previous, "2026-07-10T00:00:00Z", compressed=True)
    os.utime(current, (1783890000, 1783890000))
    os.utime(previous, (1783814400, 1783814400))
    now = datetime(2026, 7, 12, 12, tzinfo=UTC)

    assert lnav_journal.select_logs([current, previous], "2 hours ago", None, False, now) == [current]
    assert lnav_journal.select_logs([current, previous], "3 days ago", None, False, now) == [current, previous]


def test_unknown_time_syntax_falls_back_to_all_logs(tmp_path: Path) -> None:
    logs = [tmp_path / "lnav.jsonl", tmp_path / "lnav.jsonl.1"]

    assert lnav_journal.select_logs(logs, "last blue moon", None, False) == logs
