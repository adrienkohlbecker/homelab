import io
import json
from pathlib import Path

import pytest
from conftest import load_repo_module

SCRIPT = "roles/fluentbit/files/lnav_match.py"
lnav_match = load_repo_module(SCRIPT)

RECORD = {
    "service": "sonarr",
    "level": "warn",
    "message": "marker",
    "fields": {"source": "Verify", "exception": "System.Error: failed\n   at Verify.Run()", "count": 0},
}


@pytest.mark.parametrize(
    ("matcher", "expected"),
    [
        ({"service": "sonarr", "fields.source": "Verify"}, True),
        ({"fields.source": "Other"}, False),
        ({"fields.missing": None}, False),
        ({"fields.count": 0}, True),
        ({"_absent": ["fields.CONTAINER_TAG", "parse_error"]}, True),
        ({"_absent": ["fields.source"]}, False),
        ({"_present": ["fields.count"]}, True),
        ({"_present": ["fields.CONTAINER_ID"]}, False),
        ({"_contains": {"fields.exception": "System.Error"}}, True),
        ({"_contains": {"fields.exception": ["System.Error: failed", "at Verify.Run()"]}}, True),
        ({"_contains": {"fields.exception": ["System.Error", "at Other()"]}}, False),
        ({"_contains": {"fields.count": "0"}}, False),
        ({"_contains": {"fields.missing": "x"}}, False),
    ],
)
def test_matches(matcher: dict, expected: bool) -> None:
    assert lnav_match.matches(RECORD, matcher) is expected


def test_check_requires_every_expectation(capsys: pytest.CaptureFixture[str]) -> None:
    records = [RECORD, {"service": "other"}]

    assert lnav_match.check(records, [{"service": "sonarr"}, {"service": "other"}], []) == 0
    assert lnav_match.check(records, [{"service": "sonarr"}, {"service": "absent"}], []) == 1
    assert json.loads(capsys.readouterr().err) == {"unmatched": [{"service": "absent"}]}


def test_check_reports_unexpected_records_as_permanent() -> None:
    assert lnav_match.check([RECORD], [{"service": "sonarr"}], [{"level": "warn"}]) == 2
    assert lnav_match.check([RECORD], [{"service": "absent"}], [{"level": "info"}]) == 1


def test_main_skips_partial_lines_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(RECORD) + "\n{not json\n"))

    assert lnav_match.main(["-", json.dumps([{"message": "marker"}])]) == 0


def test_script_has_no_jinja_delimiters() -> None:
    source = (Path(__file__).parent.parent / SCRIPT).read_text(encoding="utf-8")

    assert not any(token in source for token in ("{{", "{%", "{#"))
