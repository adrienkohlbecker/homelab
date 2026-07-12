-- Unit tests for lnav_shape.lua. Run via `mise run test:fluentbit-lua`
-- or directly with lua5.4.

local here = arg[0]:match("^(.*/)") or "./"
dofile(here .. "lnav_shape.lua")

local failures = 0

local function check(label, got, want)
    if got ~= want then
        failures = failures + 1
        print(string.format("FAIL  %s\n        got:  %s\n        want: %s", label, tostring(got), tostring(want)))
    end
end

local function shape(tag, record, level)
    record = record or {}
    record["_level"] = level
    local _, _, shaped = shape_lnav(tag, 0, record)
    return shaped
end

do
    local rec = shape("svc.homeassistant.service", {
        host = "lab",
        log = "WARNING connection failed",
        SYSTEMD_UNIT = "homeassistant.service",
        SYSLOG_IDENTIFIER = "conmon",
        CONTAINER_NAME = "homeassistant",
        CONTAINER_TAG = "homeassistant",
        CONTAINER_ID_FULL = "123456789abcdef0",
    }, "warn")
    check("journald.host", rec.host, "lab")
    check("journald.service", rec.service, "homeassistant")
    check("journald.unit", rec.unit, "homeassistant.service")
    check("journald.identifier", rec.identifier, "conmon")
    check("journald.level", rec.level, "warn")
    check("journald.message", rec.message, "WARNING connection failed")
    check("journald.stream", rec.stream, "journald")
    check("journald.fields.systemd_unit", rec.fields.SYSTEMD_UNIT, nil)
    check("journald.fields.container_name", rec.fields.CONTAINER_NAME, nil)
    check("journald.fields.container_id_full", rec.fields.CONTAINER_ID_FULL, nil)
    check("journald.fields.no_log", rec.fields.log, nil)
end

do
    local rec = shape("svc.keepalived.service", { SYSLOG_IDENTIFIER = "Keepalived_vrrp", host = "lab" }, "info")
    check("svc.identifier_lowercase", rec.service, "keepalived_vrrp")
end

do
    local rec = shape("svc.", { SYSLOG_IDENTIFIER = "kernel" }, "info")
    check("svc.kernel", rec.service, "kernel")
end

do
    local rec =
        shape("svc.session-3.scope", { SYSLOG_IDENTIFIER = "python3(mitogen:ak@lab:12345)", log = "ok" }, "info")
    check("mitogen.service", rec.service, "mitogen")
end

do
    local rec = shape("svc.libpod-conmon-abc123.scope", { SYSLOG_IDENTIFIER = "epic_allen", log = "ok" }, "info")
    check("podman_unnamed.service", rec.service, "podman_unnamed")
end

do
    local rec = shape("svc.cron.service", {}, "info")
    check("svc.unit_fallback", rec.service, "cron")
end

do
    -- A line that did NOT match nginx_access_custom (no status field): the
    -- shaper flags it parse_error and raises it to warn so the drifting parser
    -- is visible. The raw line stays the message.
    local rec =
        shape("nginx.access", { host = "lab", log = "GET / HTTP/1.1", filepath = "/var/log/nginx/access.log" }, "info")
    check("nginx.service", rec.service, "nginx_access")
    check("nginx.stream", rec.stream, "nginx")
    check("nginx.filepath", rec.fields.filepath, "/var/log/nginx/access.log")
    check("nginx.unparsed.parse_error", rec.parse_error, "nginx_access_custom")
    check("nginx.unparsed.level", rec.level, "warn")
end

do
    -- Service-owned parsers can mark malformed records before the common
    -- shaper runs. Preserve that marker at the top level for easy querying,
    -- but do not duplicate it under fields.
    local rec = shape("svc.jellyfin.service", {
        host = "lab",
        log = "not json",
        parse_error = "jellyfin_json",
    }, "warn")
    check("service_parser.parse_error", rec.parse_error, "jellyfin_json")
    check("service_parser.fields.no_parse_error", rec.fields.parse_error, nil)
    check("service_parser.level", rec.level, "warn")
end

do
    -- Parsed access record (fields set by the nginx_access_custom parser):
    -- parsed keys land in fields, status stays an integer, a 2xx keeps the
    -- pinned debug level.
    local rec = shape("nginx.access", {
        host = "lab",
        log = '203.0.113.5 - - [08/Jun/2026:12:00:00 +0200] g.example "GET /ok HTTP/2.0" g.example 200 1 2 0.01 0.01 "-" "curl/8" "" "" 0 TLSv1.3 X',
        method = "GET",
        path = "/ok",
        status = 200,
        http_host = "g.example",
    }, "debug")
    check("nginx.access.level", rec.level, "debug")
    check("nginx.access.fields.method", rec.fields.method, "GET")
    check("nginx.access.fields.path", rec.fields.path, "/ok")
    check("nginx.access.fields.status", rec.fields.status, 200)
    check("nginx.access.fields.http_host", rec.fields.http_host, "g.example")
    -- A successful parse carries no parse_error flag.
    check("nginx.access.no_parse_error", rec.parse_error, nil)
end

do
    -- A 5xx status promotes the record to error level even though the upstream
    -- modify filter pinned it to debug; a 4xx must NOT (stays debug).
    local rec5xx = shape("nginx.access", { host = "lab", log = "x", status = 503 }, "debug")
    check("nginx.access.5xx.level", rec5xx.level, "error")
    check("nginx.access.5xx.fields.status", rec5xx.fields.status, 503)

    local rec4xx = shape("nginx.access", { host = "lab", log = "x", status = 404 }, "debug")
    check("nginx.access.4xx.level", rec4xx.level, "debug")
end

do
    local rec = shape("nginx.error", {}, "info")
    check("nginx.error", rec.service, "nginx_error")
end

do
    local rec = shape("nginx.", {}, "info")
    check("nginx.bare", rec.service, "unknown")
end

do
    local cid = "75ca2e2b110c2a3e6af421033e99bc1dbc8f58d3eacf929cb2b395377d63e4bc"
    local rec = shape("svc.init.scope", { SYSLOG_IDENTIFIER = "systemd", UNIT = cid .. ".service", log = cid }, "info")
    check("healthcheck.service", rec.service, "podman_healthcheck")
    check("healthcheck.no_cid_full", rec.fields.CONTAINER_ID_FULL, nil)
    check("healthcheck.cid_short", rec.fields.CONTAINER_ID, string.sub(cid, 1, 12))
    check("healthcheck.unit", rec.fields.UNIT, cid .. ".service")
end

do
    local rec =
        shape("host_packages", { host = "lab", log = "  updated\n\npackage \t\n", severity_text = "debug" }, nil)
    check("fallback.level", rec.level, "info")
    check("message.trim_trailing_whitespace", rec.message, "  updated\n\npackage")
    check("fields.no_severity_text", rec.fields.severity_text, nil)
end

do
    local rec = shape("plex.file", {
        host = "lab",
        log = "library scan completed",
        _service = "plex",
        _stream = "plex_file",
        _unit = "plex.service",
        _identifier = "plex",
    }, "info")
    check("override.service", rec.service, "plex")
    check("override.stream", rec.stream, "plex_file")
    check("override.unit", rec.unit, "plex.service")
    check("override.identifier", rec.identifier, "plex")
    check("override.fields.service", rec.fields._service, nil)
    check("override.fields.stream", rec.fields._stream, nil)
    check("override.fields.unit", rec.fields._unit, nil)
    check("override.fields.identifier", rec.fields._identifier, nil)
end

if failures == 0 then
    print("lnav_shape: all assertions passed")
    os.exit(0)
else
    print(string.format("lnav_shape: %d assertion(s) failed", failures))
    os.exit(1)
end
