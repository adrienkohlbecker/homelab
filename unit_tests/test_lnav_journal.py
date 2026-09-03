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

    assert parsed == (
        lnav_journal.TimeBound("2026-07-12T10:00:00+00:00", datetime(2026, 7, 12, 10, tzinfo=UTC).timestamp()),
        None,
        True,
        ["ssh"],
        ["-S", "2026-07-12T10:00:00+00:00", "-n"],
    )


def test_parse_args_normalizes_equals_bounds_against_one_clock() -> None:
    now = datetime(2026, 7, 12, 12, 34, 56, tzinfo=UTC)

    parsed = lnav_journal.parse_args(["--since=10 days ago", "--until=now"], now)

    assert parsed == (
        lnav_journal.TimeBound("2026-07-02T12:34:56+00:00", datetime(2026, 7, 2, 12, 34, 56, tzinfo=UTC).timestamp()),
        lnav_journal.TimeBound("2026-07-12T12:34:56+00:00", now.timestamp()),
        False,
        [],
        ["--since=2026-07-02T12:34:56+00:00", "--until=2026-07-12T12:34:56+00:00"],
    )


def test_parse_args_preserves_unknown_lnav_time_syntax() -> None:
    parsed = lnav_journal.parse_args(["-S", "last blue moon"], datetime(2026, 7, 12, tzinfo=UTC))

    assert parsed[-1] == ["-S", "last blue moon"]


def test_relative_bound_uses_the_same_precision_for_lnav_and_selection() -> None:
    now = datetime(2026, 7, 12, 12, 0, 0, 123456, tzinfo=UTC)

    parsed = lnav_journal.parse_args(["-S", "1 hours ago"], now)

    assert parsed[-1] == ["-S", "2026-07-12T11:00:00+00:00"]
    assert parsed[0].epoch == datetime.fromisoformat(parsed[-1][1]).timestamp()


def test_line_count_mode_is_rejected() -> None:
    with pytest.raises(SystemExit):
        lnav_journal.parse_args(["-n", "100"])


def test_unit_filter_normalizes_names_and_preserves_globs() -> None:
    expression = lnav_journal.unit_filter(["homeassistant", "session-*.scope", "odd'name.service"])

    assert expression == (
        ":unit = 'homeassistant.service' OR :unit GLOB 'session-*.scope' OR :unit = 'odd''name.service'"
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

    assert lnav_journal.select_logs([current, previous], lnav_journal.parse_bound("2 hours ago", now), None, False) == [
        current
    ]
    assert lnav_journal.select_logs([current, previous], lnav_journal.parse_bound("3 days ago", now), None, False) == [
        current,
        previous,
    ]


def test_unknown_time_syntax_falls_back_to_all_logs(tmp_path: Path) -> None:
    logs = [tmp_path / "lnav.jsonl", tmp_path / "lnav.jsonl.1"]

    bound = lnav_journal.parse_bound("last blue moon", datetime(2026, 7, 12, tzinfo=UTC))
    assert lnav_journal.select_logs(logs, bound, None, False) == logs


@pytest.mark.parametrize("option", ["-S", "--since", "--since=", "-U", "--until", "--until="])
def test_native_negative_bounds_pass_through(option: str) -> None:
    argv = [option + "-15m"] if option.endswith("=") else [option, "-15m"]

    since, until, _, _, forwarded = lnav_journal.parse_args(argv)

    assert forwarded == argv
    assert (since or until) == lnav_journal.TimeBound("-15m", None)


def test_argument_order_and_delimiter_are_preserved() -> None:
    argv = [
        "-c",
        ":goto 0",
        "--unit=ssh",
        "-S",
        "2026-07-12",
        "-u",
        "mosquitto",
        "--until=2026-07-13",
        "--since=2026-07-11",
        "-n",
        "--",
        "-u",
        "native",
        "--all",
    ]

    since, until, include_all, units, forwarded = lnav_journal.parse_args(argv)

    assert since.value == "2026-07-11"
    assert until.value == "2026-07-13"
    assert not include_all
    assert units == ["ssh", "mosquitto"]
    assert forwarded == [
        "-c",
        ":goto 0",
        "-S",
        "2026-07-12",
        "--until=2026-07-13",
        "--since=2026-07-11",
        "-n",
        "-u",
        "native",
        "--all",
    ]


@pytest.mark.parametrize("option", ["-S", "--since", "-U", "--until", "-u", "--unit"])
def test_missing_option_values_are_rejected(option: str) -> None:
    with pytest.raises(SystemExit, match="2"):
        lnav_journal.parse_args([option])


@pytest.mark.parametrize("argv", [["--lines", "100"], ["--lines=100"]])
def test_long_line_count_options_are_rejected(argv: list[str]) -> None:
    with pytest.raises(SystemExit, match="2"):
        lnav_journal.parse_args(argv)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1 second ago", "2026-07-12T12:34:55+00:00"),
        ("2 minutes ago", "2026-07-12T12:32:56+00:00"),
        ("2 HOURS ago", "2026-07-12T10:34:56+00:00"),
        ("1 day ago", "2026-07-11T12:34:56+00:00"),
        ("2 weeks ago", "2026-06-28T12:34:56+00:00"),
        ("today", "2026-07-12T00:00:00+00:00"),
        ("yesterday", "2026-07-11T00:00:00+00:00"),
        ("now", "2026-07-12T12:34:56+00:00"),
        ("2026-07-12T14:34:56.123+02:00", "2026-07-12T14:34:56.123+02:00"),
    ],
)
def test_prepared_bounds_keep_the_forwarded_time(value: str, expected: str) -> None:
    bound = lnav_journal.parse_bound(value, datetime(2026, 7, 12, 12, 34, 56, 800000, tzinfo=UTC))

    assert bound.value == expected
    assert bound.epoch == datetime.fromisoformat(expected).timestamp()


def test_rotation_at_fractional_since_boundary_is_not_lost(tmp_path: Path) -> None:
    current = tmp_path / "lnav.jsonl"
    previous = tmp_path / "lnav.jsonl.1"
    write_record(current, "2026-07-12T12:00:00Z")
    write_record(previous, "2026-07-12T11:00:00Z")
    end = datetime(2026, 7, 12, 11, 0, 0, 400000, tzinfo=UTC).timestamp()
    os.utime(previous, (end, end))
    since, _, _, _, _ = lnav_journal.parse_args(
        ["-S", "1 hour ago"], datetime(2026, 7, 12, 12, 0, 0, 800000, tzinfo=UTC)
    )

    assert lnav_journal.select_logs([current, previous], since, None, False) == [current, previous]


def test_fractional_until_boundary_excludes_later_file(tmp_path: Path) -> None:
    current = tmp_path / "lnav.jsonl"
    write_record(current, "2026-07-12T12:00:00.400Z")
    _, until, _, _, _ = lnav_journal.parse_args(["-U", "now"], datetime(2026, 7, 12, 12, 0, 0, 800000, tzinfo=UTC))

    assert lnav_journal.select_logs([current], None, until, False) == []
