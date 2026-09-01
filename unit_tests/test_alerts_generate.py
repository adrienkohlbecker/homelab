"""Unit tests for roles/homepage/files/alerts_generate.py.

Tests parsing, rendering, and the authenticated HTTP client without needing a
running netdata instance.
"""

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "roles" / "homepage" / "files" / "alerts_generate.py"


def _load():
    spec = importlib.util.spec_from_file_location("alerts_generate", _MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ag = _load()


# ---------------------------------------------------------------------------
# parse_hosts
# ---------------------------------------------------------------------------


class TestParseHosts:
    def test_name_url_format(self) -> None:
        result = ag.parse_hosts("lab=http://localhost:19999")
        assert result == [("lab", "http://localhost:19999")]

    def test_multiple_hosts(self) -> None:
        result = ag.parse_hosts("lab=http://a,pug=https://b")
        assert len(result) == 2
        assert result[0][0] == "lab"
        assert result[1][0] == "pug"

    def test_strips_trailing_slash(self) -> None:
        result = ag.parse_hosts("lab=https://b/")
        assert result[0][1] == "https://b"

    def test_empty_string(self) -> None:
        assert ag.parse_hosts("") == []

    def test_blank_entries_skipped(self) -> None:
        result = ag.parse_hosts(",lab=http://a,,")
        assert len(result) == 1

    def test_url_with_equals(self) -> None:
        result = ag.parse_hosts("lab=https://cloud.netdata.cloud/spaces?id=1")
        assert result[0][1] == "https://cloud.netdata.cloud/spaces?id=1"

    def test_whitespace_stripped(self) -> None:
        result = ag.parse_hosts("  lab = http://a  ")
        assert result[0] == ("lab", "http://a")

    @pytest.mark.parametrize("spec", ["lab", "=https://netdata.lab", "lab="])
    def test_malformed_entry_rejected(self, spec: str) -> None:
        with pytest.raises(ValueError, match="malformed NETDATA_HOSTS entry"):
            ag.parse_hosts(spec)


# ---------------------------------------------------------------------------
# authenticated HTTP client
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.status = 200
        self._body = body
        self.closed = False

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        self.closed = True


class TestHostClient:
    def test_sends_basic_auth_to_both_netdata_endpoints(self, monkeypatch: pytest.MonkeyPatch) -> None:
        responses = iter([_FakeResponse(b'{"alarms": {}}'), _FakeResponse(b'{"transitions": []}')])
        requests: list[tuple[str, str, dict[str, str]]] = []

        class FakeConnection:
            def __init__(self, host: str, **kwargs: object) -> None:
                assert host == "netdata.lab.fahm.fr"
                assert kwargs["timeout"] == ag.FETCH_TIMEOUT

            def request(self, method: str, path: str, headers: dict[str, str]) -> None:
                requests.append((method, path, headers))

            def getresponse(self) -> _FakeResponse:
                return next(responses)

            def close(self) -> None:
                pass

        monkeypatch.setattr(ag.http.client, "HTTPSConnection", FakeConnection)
        authorization = ag._basic_authorization("homepage_alerts", "secret")
        client = ag._HostClient("https://netdata.lab.fahm.fr", authorization)

        assert client.alarms() == {"alarms": {}}
        assert client.alert_transitions() == {"transitions": []}
        assert [request[1] for request in requests] == [
            "/api/v1/alarms?active",
            "/api/v2/alert_transitions?after=-604800&last=10000",
        ]
        assert all(request[2]["Authorization"] == "Basic aG9tZXBhZ2VfYWxlcnRzOnNlY3JldA==" for request in requests)

    def test_rejects_cleartext_http(self) -> None:
        with pytest.raises(OSError, match="refusing to send Basic authentication"):
            ag._HostClient("http://netdata.lab.fahm.fr", "Basic credential")


# ---------------------------------------------------------------------------
# _fetch_one
# ---------------------------------------------------------------------------


class TestFetchOne:
    def test_transition_history_miss_uses_alerts_list_without_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        class FakeClient:
            def __init__(self, url: str, authorization: str) -> None:
                pass

            def alarms(self) -> dict:
                return {
                    "alarms": {
                        "system.cpu": {
                            "id": 1,
                            "name": "cpu_high",
                            "chart": "system.cpu",
                            "status": "WARNING",
                        }
                    }
                }

            def alert_transitions(self) -> dict:
                return {"transitions": []}

            def close(self) -> None:
                pass

        monkeypatch.setattr(ag, "_HostClient", FakeClient)

        result = ag._fetch_one("lab", "https://netdata.lab", "Basic credential")

        assert result["alarms"][0]["href"] == "https://netdata.lab/v2/spaces/lab/rooms/local/alerts"
        assert capsys.readouterr().err == ""

    def test_transition_parse_failure_remains_visible(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        class FakeClient:
            def __init__(self, url: str, authorization: str) -> None:
                pass

            def alarms(self) -> dict:
                return {"alarms": {}}

            def alert_transitions(self) -> dict:
                raise ValueError("bad payload")

            def close(self) -> None:
                pass

        monkeypatch.setattr(ag, "_HostClient", FakeClient)

        ag._fetch_one("lab", "https://netdata.lab", "Basic credential")

        assert "alert_transitions parse failed" in capsys.readouterr().err


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
    def test_envelope_newest_wins(self) -> None:
        # Same (alert, instance): the higher `gi` is the current-state transition.
        log = {
            "transitions": [
                {
                    "alert": "cpu",
                    "instance": "system.cpu",
                    "gi": 10,
                    "transition_id": "tid-old",
                },
                {
                    "alert": "cpu",
                    "instance": "system.cpu",
                    "gi": 20,
                    "transition_id": "tid-new",
                },
            ]
        }
        result = ag.latest_transition_by_alarm(log)
        assert result == {("cpu", "system.cpu"): "tid-new"}

    def test_bare_list_accepted(self) -> None:
        log = [
            {
                "alert": "cpu",
                "instance": "system.cpu",
                "gi": 1,
                "transition_id": "tid-a",
            }
        ]
        result = ag.latest_transition_by_alarm(log)
        assert result == {("cpu", "system.cpu"): "tid-a"}

    def test_multiple_alarms(self) -> None:
        log = {
            "transitions": [
                {"alert": "a1", "instance": "c1", "gi": 5, "transition_id": "t1"},
                {"alert": "a2", "instance": "c2", "gi": 10, "transition_id": "t2"},
            ]
        }
        result = ag.latest_transition_by_alarm(log)
        assert result == {("a1", "c1"): "t1", ("a2", "c2"): "t2"}

    def test_when_fallback_sort(self) -> None:
        # No `gi`: fall back to `when` for newest-first ordering.
        log = {
            "transitions": [
                {
                    "alert": "a",
                    "instance": "c",
                    "when": 100,
                    "transition_id": "tid-old",
                },
                {
                    "alert": "a",
                    "instance": "c",
                    "when": 200,
                    "transition_id": "tid-new",
                },
            ]
        }
        assert ag.latest_transition_by_alarm(log) == {("a", "c"): "tid-new"}

    def test_empty_log(self) -> None:
        assert ag.latest_transition_by_alarm([]) == {}
        assert ag.latest_transition_by_alarm({"transitions": []}) == {}
        assert ag.latest_transition_by_alarm({}) == {}

    def test_missing_fields_skipped(self) -> None:
        log = {
            "transitions": [
                {"alert": "a", "instance": "c"},  # no transition_id
                {"instance": "c", "gi": 1, "transition_id": "t"},  # no alert
                {"alert": "a", "gi": 2, "transition_id": "t"},  # no instance
            ]
        }
        assert ag.latest_transition_by_alarm(log) == {}


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
        assert result == "2m ago"

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
        hosts = [{"name": "pug", "alarms": []}]
        html = ag.render_html(hosts, "2024-01-01T00:00:00+00:00")
        assert "No active alerts" in html

    def test_renders_error(self) -> None:
        hosts = [
            {
                "name": "lab",
                "error": "ConnectionError",
                "alarms": [],
            }
        ]
        html = ag.render_html(hosts, "2024-01-01T00:00:00+00:00")
        assert "ConnectionError" in html

    def test_renders_footer(self) -> None:
        html = ag.render_html([], "2024-06-01T12:00:00+00:00")
        assert "Updated 2024-06-01T12:00:00+00:00" in html

    def test_critical_has_icon(self) -> None:
        hosts = [
            {
                "name": "lab",
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
            {"name": "lab", "alarms": []},
            {"name": "pug", "alarms": []},
        ]
        html = ag.render_html(hosts, "now")
        assert html.index("lab") < html.index("pug")
