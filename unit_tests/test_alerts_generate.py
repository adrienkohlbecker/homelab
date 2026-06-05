"""Unit tests for roles/homepage/files/alerts_generate.py.

Tests the pure functions (parse_hosts, normalize, latest_transition_by_alarm,
alarm_href, _humanize_delta, _format_value, render_html) without needing
network access or a running netdata instance.
"""

import importlib
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "roles" / "homepage" / "files" / "alerts_generate.py"


def _load():
    spec = importlib.util.spec_from_file_location("alerts_generate", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ag = _load()


# ---------------------------------------------------------------------------
# parse_hosts
# ---------------------------------------------------------------------------


class TestParseHosts:
    def test_two_url_format(self) -> None:
        result = ag.parse_hosts("lab=http://localhost:19999")
        assert result == [("lab", "http://localhost:19999", "http://localhost:19999")]

    def test_three_url_format(self) -> None:
        result = ag.parse_hosts("lab=http://localhost:19999=https://netdata.lab.fahm.fr")
        assert result == [("lab", "http://localhost:19999", "https://netdata.lab.fahm.fr")]

    def test_multiple_hosts(self) -> None:
        result = ag.parse_hosts("lab=http://a,pug=https://b")
        assert len(result) == 2
        assert result[0][0] == "lab"
        assert result[1][0] == "pug"

    def test_strips_trailing_slash(self) -> None:
        result = ag.parse_hosts("lab=http://a/=https://b/")
        assert result[0][1] == "http://a"
        assert result[0][2] == "https://b"

    def test_empty_string(self) -> None:
        assert ag.parse_hosts("") == []

    def test_blank_entries_skipped(self) -> None:
        result = ag.parse_hosts(",lab=http://a,,")
        assert len(result) == 1

    def test_click_url_with_equals(self) -> None:
        result = ag.parse_hosts("lab=http://a=https://cloud.netdata.cloud/spaces?id=1")
        assert result[0][2] == "https://cloud.netdata.cloud/spaces?id=1"

    def test_whitespace_stripped(self) -> None:
        result = ag.parse_hosts("  lab = http://a  ")
        assert result[0] == ("lab", "http://a", "http://a")


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_empty_payload(self) -> None:
        assert ag.normalize({}) == []
        assert ag.normalize({"alarms": {}}) == []

    def test_sorts_critical_before_warning(self) -> None:
        payload = {
            "alarms": {
                "chart.warn": {
                    "id": 1,
                    "name": "warn",
                    "chart": "c1",
                    "status": "WARNING",
                    "value_string": "50%",
                    "last_status_change": 100,
                },
                "chart.crit": {
                    "id": 2,
                    "name": "crit",
                    "chart": "c2",
                    "status": "CRITICAL",
                    "value_string": "90%",
                    "last_status_change": 200,
                },
            }
        }
        result = ag.normalize(payload)
        assert len(result) == 2
        assert result[0]["status"] == "CRITICAL"
        assert result[1]["status"] == "WARNING"

    def test_fields_extracted(self) -> None:
        payload = {
            "alarms": {
                "sys.cpu_usage": {
                    "id": 42,
                    "name": "cpu_usage",
                    "chart": "system.cpu",
                    "status": "WARNING",
                    "value_string": "85 %",
                    "last_status_change": 1700000000,
                    "info": "CPU usage is high",
                }
            }
        }
        result = ag.normalize(payload)
        assert len(result) == 1
        a = result[0]
        assert a["key"] == "sys.cpu_usage"
        assert a["id"] == 42
        assert a["name"] == "cpu_usage"
        assert a["chart"] == "system.cpu"
        assert a["status"] == "WARNING"
        assert a["value"] == "85 %"
        assert a["when"] == 1700000000
        assert a["info"] == "CPU usage is high"

    def test_alphabetical_within_same_status(self) -> None:
        payload = {
            "alarms": {
                "z.alarm": {
                    "status": "WARNING",
                    "id": 1,
                    "name": "z",
                    "chart": "c",
                    "value_string": "1",
                    "last_status_change": 0,
                },
                "a.alarm": {
                    "status": "WARNING",
                    "id": 2,
                    "name": "a",
                    "chart": "c",
                    "value_string": "1",
                    "last_status_change": 0,
                },
            }
        }
        result = ag.normalize(payload)
        assert result[0]["key"] == "a.alarm"
        assert result[1]["key"] == "z.alarm"


# ---------------------------------------------------------------------------
# latest_transition_by_alarm
# ---------------------------------------------------------------------------


class TestLatestTransitionByAlarm:
    def test_list_format(self) -> None:
        log = [
            {"alarm_id": 1, "unique_id": 10, "transition_id": "tid-old"},
            {"alarm_id": 1, "unique_id": 20, "transition_id": "tid-new"},
        ]
        result = ag.latest_transition_by_alarm(log)
        assert result == {1: "tid-new"}

    def test_dict_envelope_format(self) -> None:
        log = {
            "data": [
                {"alarm_id": 1, "unique_id": 10, "transition_id": "tid-a"},
            ]
        }
        result = ag.latest_transition_by_alarm(log)
        assert result == {1: "tid-a"}

    def test_multiple_alarms(self) -> None:
        log = [
            {"alarm_id": 1, "unique_id": 5, "transition_id": "t1"},
            {"alarm_id": 2, "unique_id": 10, "transition_id": "t2"},
        ]
        result = ag.latest_transition_by_alarm(log)
        assert result == {1: "t1", 2: "t2"}

    def test_empty_log(self) -> None:
        assert ag.latest_transition_by_alarm([]) == {}
        assert ag.latest_transition_by_alarm({"data": []}) == {}

    def test_transition_uuid_fallback(self) -> None:
        log = [{"alarm_id": 1, "unique_id": 1, "transition_uuid": "uuid-1"}]
        result = ag.latest_transition_by_alarm(log)
        assert result == {1: "uuid-1"}

    def test_id_fallback(self) -> None:
        log = [{"id": 5, "unique_id": 1, "transition_id": "t5"}]
        result = ag.latest_transition_by_alarm(log)
        assert result == {5: "t5"}


# ---------------------------------------------------------------------------
# alarm_href
# ---------------------------------------------------------------------------


class TestAlarmHref:
    def test_with_transition_id(self) -> None:
        alarm = {
            "id": 42,
            "name": "cpu_high",
            "chart": "system.cpu",
            "status": "WARNING",
            "value": "85",
            "when": 1700000000,
            "transition_id": "abc-123",
        }
        href = ag.alarm_href("https://netdata.lab.fahm.fr", "lab", alarm)
        assert "/v2/spaces/lab/rooms/local/alerts/abc-123" in href
        assert "transition_id=abc-123" in href
        assert "alarm=cpu_high" in href

    def test_without_transition_id(self) -> None:
        alarm = {"id": 1, "name": "x", "chart": "c", "status": "WARNING"}
        href = ag.alarm_href("https://netdata.lab.fahm.fr", "lab", alarm)
        assert href == "https://netdata.lab.fahm.fr/v2/spaces/lab/rooms/local/alerts"

    def test_empty_transition_id(self) -> None:
        alarm = {"id": 1, "name": "x", "chart": "c", "transition_id": ""}
        href = ag.alarm_href("https://nd", "host", alarm)
        assert href == "https://nd/v2/spaces/host/rooms/local/alerts"


# ---------------------------------------------------------------------------
# _humanize_delta
# ---------------------------------------------------------------------------


class TestHumanizeDelta:
    def test_just_now(self) -> None:
        assert ag._humanize_delta(10) == "just now"
        assert ag._humanize_delta(-10) == "just now"

    def test_minutes(self) -> None:
        assert ag._humanize_delta(300) == "5m ago"

    def test_hours(self) -> None:
        assert ag._humanize_delta(7200) == "2h ago"

    def test_days(self) -> None:
        assert ag._humanize_delta(86400 * 3) == "3d ago"

    def test_months(self) -> None:
        assert ag._humanize_delta(86400 * 60) == "2mo ago"

    def test_years(self) -> None:
        assert ag._humanize_delta(86400 * 400) == "1y ago"

    def test_from_now(self) -> None:
        assert ag._humanize_delta(-300) == "5m from now"


# ---------------------------------------------------------------------------
# _format_value
# ---------------------------------------------------------------------------


class TestFormatValue:
    def test_regular_value(self) -> None:
        alarm = {"units": "%", "value_string": "85.3 %"}
        assert ag._format_value(alarm) == "85.3 %"

    def test_timestamp_zero(self) -> None:
        alarm = {"units": "timestamp", "value": "0", "value_string": "0 timestamp"}
        assert ag._format_value(alarm) == "never"

    def test_timestamp_recent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fixed_now = 1700000000.0
        monkeypatch.setattr(ag.time, "time", lambda: fixed_now)
        alarm = {"units": "timestamp", "value": str(fixed_now - 120)}
        result = ag._format_value(alarm)
        assert "2m ago" == result

    def test_no_units(self) -> None:
        alarm = {"value_string": "42"}
        assert ag._format_value(alarm) == "42"


# ---------------------------------------------------------------------------
# render_html
# ---------------------------------------------------------------------------


class TestRenderHtml:
    def test_renders_host_section(self) -> None:
        hosts = [
            {
                "name": "lab",
                "click_url": "https://nd",
                "alarms": [
                    {
                        "name": "cpu",
                        "chart": "system.cpu",
                        "status": "WARNING",
                        "value": "85%",
                        "href": "https://nd/alert",
                        "transition_id": "t1",
                    }
                ],
            }
        ]
        html = ag.render_html(hosts, "2024-01-01T00:00:00+00:00")
        assert "lab" in html
        assert "cpu" in html
        assert "system.cpu" in html
        assert "WARNING" in html
        assert "85%" in html
        assert 'href="https://nd/alert"' in html

    def test_renders_no_alerts(self) -> None:
        hosts = [{"name": "pug", "click_url": "https://nd", "alarms": []}]
        html = ag.render_html(hosts, "2024-01-01T00:00:00+00:00")
        assert "No active alerts" in html

    def test_renders_error(self) -> None:
        hosts = [{"name": "lab", "click_url": "https://nd", "error": "ConnectionError", "alarms": []}]
        html = ag.render_html(hosts, "2024-01-01T00:00:00+00:00")
        assert "ConnectionError" in html

    def test_renders_footer(self) -> None:
        html = ag.render_html([], "2024-06-01T12:00:00+00:00")
        assert "Updated 2024-06-01T12:00:00+00:00" in html

    def test_critical_has_icon(self) -> None:
        hosts = [
            {
                "name": "lab",
                "click_url": "https://nd",
                "alarms": [
                    {
                        "name": "x",
                        "chart": "c",
                        "status": "CRITICAL",
                        "value": "99%",
                        "href": "#",
                        "transition_id": "",
                    }
                ],
            }
        ]
        html = ag.render_html(hosts, "now")
        assert "status-icon" in html
        assert 'class="alarm CRITICAL"' in html

    def test_html_escaping(self) -> None:
        hosts = [
            {
                "name": "<b>bad</b>",
                "click_url": "https://nd",
                "alarms": [
                    {
                        "name": "x<y",
                        "chart": "a&b",
                        "status": "WARNING",
                        "value": "1>0",
                        "href": "#",
                        "transition_id": "",
                    }
                ],
            }
        ]
        html = ag.render_html(hosts, "now")
        assert "<b>bad</b>" not in html
        assert "&lt;b&gt;bad&lt;/b&gt;" in html
        assert "x&lt;y" in html
        assert "a&amp;b" in html

    def test_multiple_hosts(self) -> None:
        hosts = [
            {"name": "lab", "click_url": "#", "alarms": []},
            {"name": "pug", "click_url": "#", "alarms": []},
        ]
        html = ag.render_html(hosts, "now")
        assert html.index("lab") < html.index("pug")
