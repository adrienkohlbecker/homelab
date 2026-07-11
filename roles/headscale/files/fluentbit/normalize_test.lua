-- Unit tests model Fluent Bit's JSON parser output for representative records
-- from the retained Headscale journal corpus on fox.

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
    local code = normalize_headscale(tag or "svc.headscale.service", 0, record)
    return record, code
end

do
    local rec, code = normalize({
        log = '{"level":"info","node":"lab","node.id":15,"caller":"hscontrol/poll.go:602","time":1783803558,"message":"node has connected"}',
        CONTAINER_TAG = "headscale",
        CONTAINER_ID = "123456789abc",
        CONTAINER_ID_FULL = "123456789abcdef0",
        CMDLINE = "/usr/bin/conmon --log-tag headscale",
        PRIORITY = "6",
        SYSTEMD_UNIT = "headscale.service",
        SYSLOG_IDENTIFIER = "headscale",
        level = "info",
        node = "lab",
        ["node.id"] = 15,
        caller = "hscontrol/poll.go:602",
        time = 1783803558,
        message = "node has connected",
    })
    check("connect.code", code, 1)
    check("connect.message", rec.log, "node has connected")
    check("connect.level", rec._level, "info")
    check("connect.node", rec.node, "lab")
    check("connect.node_id", rec["node.id"], 15)
    check("connect.caller", rec.caller, "hscontrol/poll.go:602")
    check("connect.status", rec.parser_status, "parsed")
    check("connect.timestamp_removed", rec.time, nil)
    check("connect.source_level_removed", rec.level, nil)
    check("connect.source_message_removed", rec.message, nil)
    check("connect.short_container_id_kept", rec.CONTAINER_ID, "123456789abc")
    check("connect.container_tag_removed", rec.CONTAINER_TAG, nil)
    check("connect.full_container_id_removed", rec.CONTAINER_ID_FULL, nil)
    check("connect.cmdline_removed", rec.CMDLINE, nil)
    check("connect.priority_removed", rec.PRIORITY, nil)
    check("connect.unit_removed", rec.SYSTEMD_UNIT, nil)
    check("connect.identifier_removed", rec.SYSLOG_IDENTIFIER, nil)
end

do
    local rec = normalize({
        log = "raw JSON",
        CONTAINER_TAG = "headscale",
        level = "info",
        time = 1783803559,
        message = "node has disconnected",
        node = "Adrien's iPhone",
        ["node.id"] = 17,
    })
    check("disconnect.level", rec._level, "info")
    check("disconnect.message", rec.log, "node has disconnected")
    check("disconnect.node", rec.node, "Adrien's iPhone")
    check("disconnect.node_id", rec["node.id"], 17)
end

do
    local rec = normalize({
        log = "raw JSON",
        CONTAINER_TAG = "headscale",
        level = "warn",
        time = 1783803560,
        message = "Listening without TLS but ServerURL does not start with http://\n",
    })
    check("warning.level", rec._level, "warn")
    check("warning.message", rec.log, "Listening without TLS but ServerURL does not start with http://")
end

do
    local rec = normalize({
        log = "raw JSON",
        CONTAINER_TAG = "headscale",
        level = "error",
        time = 1783803561,
        message = "http internal server error",
        error = "unexpected end of JSON input",
        code = 13,
    })
    check("error.level", rec._level, "error")
    check("error.message", rec.log, "http internal server error")
    check("error.detail", rec.error, "unexpected end of JSON input")
    check("error.code", rec.code, 13)
end

do
    local rec = normalize({
        log = '{"level":"info","message":"missing time"}',
        CONTAINER_TAG = "headscale",
        level = "info",
        message = "missing time",
    })
    check("schema.status", rec.parser_status, "failed")
    check("schema.error", rec.parse_error, "headscale_schema")
    check("schema.level", rec._level, "warn")
    check("schema.raw", rec.log, '{"level":"info","message":"missing time"}')
end

do
    local rec = normalize({ log = '{"level":"info"', CONTAINER_TAG = "headscale" })
    check("json.status", rec.parser_status, "failed")
    check("json.error", rec.parse_error, "headscale_json")
    check("json.level", rec._level, "warn")
    check("json.raw", rec.log, '{"level":"info"')
end

do
    local rec = normalize({
        log = "raw JSON",
        CONTAINER_TAG = "headscale",
        level = "notice",
        time = 1783803562,
        message = "unknown level",
    })
    check("level.status", rec.parser_status, "failed")
    check("level.error", rec.parse_error, "headscale_level")
    check("level.level", rec._level, "warn")
end

do
    local line = "2026-07-11T20:59:18Z INF hscontrol/poll.go:602 > node has connected node=lab"
    local rec = normalize({ log = line, CONTAINER_TAG = "headscale" })
    check("legacy.status", rec.parser_status, "skipped")
    check("legacy.reason", rec.parser_reason, "non_json")
    check("legacy.raw", rec.log, line)
    check("legacy.error", rec.parse_error, nil)
end

do
    local rec, code = normalize({ log = "untouched" }, "svc.other.service")
    check("tag.code", code, 0)
    check("tag.status", rec.parser_status, nil)
end

do
    local rec, code = normalize({ log = "podman command output" })
    check("unit_output.code", code, 0)
    check("unit_output.status", rec.parser_status, nil)
    check("unit_output.raw", rec.log, "podman command output")
end

if failures > 0 then
    os.exit(1)
end
print("PASS  headscale normalize")
