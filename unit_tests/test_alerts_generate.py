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
    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("lab=http://localhost:19999", [("lab", "http://localhost:19999")]),
            ("lab=http://a,pug=https://b", [("lab", "http://a"), ("pug", "https://b")]),
            ("lab=https://b/", [("lab", "https://b")]),
            ("", []),
            (",lab=http://a,,", [("lab", "http://a")]),
            (
                "lab=https://cloud.netdata.cloud/spaces?id=1",
                [("lab", "https://cloud.netdata.cloud/spaces?id=1")],
            ),
            ("  lab = http://a  ", [("lab", "http://a")]),
        ],
    )
    def test_valid_entries(self, spec: str, expected: list[tuple[str, str]]) -> None:
        assert ag.parse_hosts(spec) == expected

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
    def test_latest_transition_builds_deep_link(
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
                return {
                    "transitions": [
                        {
                            "alert": "cpu_high",
                            "instance": "system.cpu",
                            "gi": 10,
                            "transition_id": "tid-old",
                        },
                        {
                            "alert": "cpu_high",
                            "instance": "system.cpu",
                            "gi": 20,
                            "transition_id": "tid-new",
                        },
                    ]
                }

            def close(self) -> None:
                pass

        monkeypatch.setattr(ag, "_HostClient", FakeClient)

        result = ag._fetch_one("lab", "https://netdata.lab", "Basic credential")

        assert "/alerts/tid-new?" in result["alarms"][0]["href"]
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
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (10, "just now"),
            (-10, "just now"),
            (300, "5m ago"),
            (7200, "2h ago"),
            (86400 * 3, "3d ago"),
            (86400 * 60, "2mo ago"),
            (86400 * 400, "1y ago"),
            (-300, "5m from now"),
        ],
    )
    def test_formats_delta(self, seconds: float, expected: str) -> None:
        assert ag._humanize_delta(seconds) == expected


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
        html = ag.render_html(hosts)
        assert "lab" in html
        assert "cpu" in html
        assert "system.cpu" in html
        assert "WARNING" in html
        assert "85%" in html
        assert 'href="https://nd/alert"' in html

    def test_renders_no_alerts(self) -> None:
        hosts = [{"name": "pug", "alarms": []}]
        html = ag.render_html(hosts)
        assert "No active alerts" in html

    def test_renders_error(self) -> None:
        hosts = [
            {
                "name": "lab",
                "error": "ConnectionError",
                "alarms": [],
            }
        ]
        html = ag.render_html(hosts)
        assert "ConnectionError" in html

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
        html = ag.render_html(hosts)
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
        html = ag.render_html(hosts)
        assert "<b>bad</b>" not in html
        assert "&lt;b&gt;bad&lt;/b&gt;" in html
        assert "x&lt;y" in html
        assert "a&amp;b" in html

    def test_multiple_hosts(self) -> None:
        hosts = [
            {"name": "lab", "alarms": []},
            {"name": "pug", "alarms": []},
        ]
        html = ag.render_html(hosts)
        assert html.index("lab") < html.index("pug")
