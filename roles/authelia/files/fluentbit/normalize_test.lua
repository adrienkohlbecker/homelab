-- Unit tests for Authelia's post-JSON Fluent Bit normalizer. The records model
-- the built-in parser's Reserve_Data and Preserve_Key output.

local here = arg[0]:match("^(.*/)") or "./"
dofile(here .. "normalize.lua")

local failures = 0

local function check(label, got, want)
    if got ~= want then
        failures = failures + 1
        print(string.format("FAIL  %s\n        got:  %s\n        want: %s", label, tostring(got), tostring(want)))
    end
end

local function normalize(record, tag)
    local code = normalize_authelia(tag or "svc.authelia.service", 0, record)
    return record, code
end

do
    local rec, code = normalize({
        log = '{"level":"info","method":"GET","msg":"Access denied","path":"/api/authz/auth-request","remote_ip":"100.64.0.1","time":"2026-07-11T20:00:05Z"}',
        CONTAINER_TAG = "authelia",
        CONTAINER_ID = "123456789abc",
        CONTAINER_ID_FULL = "123456789abcdef0",
        CMDLINE = "/usr/bin/conmon --log-tag authelia",
        PRIORITY = "6",
        SYSTEMD_UNIT = "authelia.service",
        SYSLOG_IDENTIFIER = "authelia",
        time = "2026-07-11T20:00:05Z",
        level = "info",
        msg = "Access denied",
        method = "GET",
        path = "/api/authz/auth-request",
        remote_ip = "100.64.0.1",
    })
    check("info.code", code, 1)
    check("info.message", rec.log, "Access denied")
    check("info.level", rec._level, "info")
    check("info.method", rec.method, "GET")
    check("info.path", rec.path, "/api/authz/auth-request")
    check("info.remote_ip", rec.remote_ip, "100.64.0.1")
    check("info.status", rec.parser_status, "parsed")
    check("info.timestamp_removed", rec.time, nil)
    check("info.source_level_removed", rec.level, nil)
    check("info.source_message_removed", rec.msg, nil)
    check("info.short_container_id_kept", rec.CONTAINER_ID, "123456789abc")
    check("info.container_tag_removed", rec.CONTAINER_TAG, nil)
    check("info.full_container_id_removed", rec.CONTAINER_ID_FULL, nil)
    check("info.cmdline_removed", rec.CMDLINE, nil)
    check("info.priority_removed", rec.PRIORITY, nil)
    check("info.unit_removed", rec.SYSTEMD_UNIT, nil)
    check("info.identifier_removed", rec.SYSLOG_IDENTIFIER, nil)
end

do
    local rec = normalize({
        log = "raw JSON",
        CONTAINER_TAG = "authelia",
        time = "2026-07-11T20:01:00Z",
        level = "warning",
        msg = "Could not determine the clock offset",
        error = "dial udp: connection refused",
    })
    check("warning.level", rec._level, "warn")
    check("warning.error", rec.error, "dial udp: connection refused")
end

do
    local rec = normalize({
        log = "raw JSON",
        CONTAINER_TAG = "authelia",
        time = "2026-07-11T20:02:00Z",
        level = "fatal",
        msg = "Provider startup failed\n",
        provider = "notification",
        stack = "root.go:101\ncommand.go:90",
    })
    check("fatal.level", rec._level, "fatal")
    check("fatal.message", rec.log, "Provider startup failed")
    check("fatal.provider", rec.provider, "notification")
    check("fatal.stack", rec.stack, "root.go:101\ncommand.go:90")
end

do
    local rec = normalize({
        log = "raw JSON",
        CONTAINER_TAG = "authelia",
        time = "2026-07-11T20:03:00Z",
        level = "info",
        msg = "OpenID Connect 1.0 client requires 2FA",
        client_id = "headscale",
        flow = "openid_connect",
        flow_id = "4fd12f85-f83a-4b79-b469-f6fa70ac3d35",
        username = "akohlbecker",
    })
    check("oidc.client", rec.client_id, "headscale")
    check("oidc.flow", rec.flow, "openid_connect")
    check("oidc.flow_id", rec.flow_id, "4fd12f85-f83a-4b79-b469-f6fa70ac3d35")
    check("oidc.username", rec.username, "akohlbecker")
end

do
    local rec = normalize({
        log = '{"level":"info","time":"2026-07-11T20:04:00Z"}',
        CONTAINER_TAG = "authelia",
        time = "2026-07-11T20:04:00Z",
        level = "info",
    })
    check("schema.status", rec.parser_status, "failed")
    check("schema.error", rec.parse_error, "authelia_schema")
    check("schema.level", rec._level, "warn")
end

do
    local rec = normalize({ log = '{"time":"broken"', CONTAINER_TAG = "authelia" })
    check("json.status", rec.parser_status, "failed")
    check("json.error", rec.parse_error, "authelia_json")
    check("json.raw", rec.log, '{"time":"broken"')
end

do
    local rec = normalize({ log = "unexpected child output", CONTAINER_TAG = "authelia" })
    check("plain.status", rec.parser_status, "skipped")
    check("plain.reason", rec.parser_reason, "non_json")
    check("plain.raw", rec.log, "unexpected child output")
end

do
    local rec, code = normalize({ log = "podman command output" })
    check("unit_output.code", code, 0)
    check("unit_output.status", rec.parser_status, nil)
    check("unit_output.raw", rec.log, "podman command output")
end

do
    local rec, code = normalize({ log = "untouched", CONTAINER_TAG = "authelia" }, "svc.other.service")
    check("tag.code", code, 0)
    check("tag.container_tag", rec.CONTAINER_TAG, "authelia")
end

if failures > 0 then
    os.exit(1)
end
print("PASS  authelia normalize")
