-- Unit tests for Zigbee2MQTT's post-JSON Fluent Bit normalizer. Parsed-record
-- fixtures model the built-in JSON parser with Preserve_Key On.

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
    local code = normalize_z2m(tag or "svc.z2m.service", 0, record)
    return record, code
end

do
    local rec, code = normalize({
        log = '{"level":"warning","message":"z2m: Failed to ping device","timestamp":"2026-07-11 12:00:00"}',
        level = "warning",
        message = "z2m: Failed to ping device",
        timestamp = "2026-07-11 12:00:00",
        CONTAINER_TAG = "z2m",
        CONTAINER_ID = "123456789abc",
        CONTAINER_ID_FULL = "123456789abcdef0",
        CMDLINE = "/usr/bin/conmon --log-tag z2m",
        PRIORITY = "6",
        SYSTEMD_UNIT = "z2m.service",
        SYSLOG_IDENTIFIER = "z2m",
    })
    check("warning.code", code, 1)
    check("warning.message", rec.log, "Failed to ping device")
    check("warning.level", rec._level, "warn")
    check("warning.namespace", rec.namespace, "z2m")
    check("warning.status", rec.parser_status, "parsed")
    check("warning.timestamp_removed", rec.timestamp, nil)
    check("warning.source_level_removed", rec.level, nil)
    check("warning.source_message_removed", rec.message, nil)
    check("warning.short_container_id_kept", rec.CONTAINER_ID, "123456789abc")
    check("warning.container_tag_removed", rec.CONTAINER_TAG, nil)
    check("warning.full_container_id_removed", rec.CONTAINER_ID_FULL, nil)
    check("warning.cmdline_removed", rec.CMDLINE, nil)
    check("warning.priority_removed", rec.PRIORITY, nil)
    check("warning.unit_removed", rec.SYSTEMD_UNIT, nil)
    check("warning.identifier_removed", rec.SYSLOG_IDENTIFIER, nil)
end

do
    local rec = normalize({
        log = "raw JSON",
        level = "error",
        message = "zh:zstack:znp: Socket error Error: connect EHOSTUNREACH 10.123.4.16:6638",
        timestamp = "2026-07-11 12:01:00",
        stack = "Error: socket failure\n    at Socket.connect (net.js:1:2)",
        CONTAINER_TAG = "z2m",
    })
    check("error.message", rec.log, "Socket error Error: connect EHOSTUNREACH 10.123.4.16:6638")
    check("error.level", rec._level, "error")
    check("error.namespace", rec.namespace, "zh:zstack:znp")
    check("error.exception", rec.exception, "Error: socket failure\n    at Socket.connect (net.js:1:2)")
    check("error.stack_removed", rec.stack, nil)
end

do
    local rec = normalize({
        log = '{"level":"warning","timestamp":"2026-07-11 12:02:00"}',
        level = "warning",
        timestamp = "2026-07-11 12:02:00",
        CONTAINER_TAG = "z2m",
    })
    check("schema.status", rec.parser_status, "failed")
    check("schema.error", rec.parse_error, "z2m_schema")
    check("schema.level", rec._level, "warn")
end

do
    local rec = normalize({ log = '{"level":"warning"', CONTAINER_TAG = "z2m" })
    check("json.status", rec.parser_status, "failed")
    check("json.error", rec.parse_error, "z2m_json")
    check("json.raw", rec.log, '{"level":"warning"')
end

do
    local rec = normalize({
        log = '{"level":"notice","message":"z2m: Future level","timestamp":"2026-07-11 12:03:00"}',
        level = "notice",
        message = "z2m: Future level",
        timestamp = "2026-07-11 12:03:00",
        CONTAINER_TAG = "z2m",
    })
    check("level.status", rec.parser_status, "failed")
    check("level.error", rec.parse_error, "z2m_level")
end

for label, line in pairs({
    data_directory = "Using '/app/data' as data directory\n",
    watchdog = "Starting Zigbee2MQTT without watchdog.\n",
}) do
    local rec = normalize({ log = line, CONTAINER_TAG = "z2m", PRIORITY = "6" })
    check(label .. ".message", rec.log, line:gsub("[\r\n]+$", ""))
    check(label .. ".status", rec.parser_status, "skipped")
    check(label .. ".reason", rec.parser_reason, "non_json")
    check(label .. ".priority_removed", rec.PRIORITY, nil)
end

do
    local record = { log = "podman command output" }
    local rec, code = normalize(record)
    check("unit_output.code", code, 0)
    check("unit_output.status", rec.parser_status, nil)
    check("unit_output.raw", rec.log, "podman command output")
end

do
    local rec, code = normalize({ log = "untouched", CONTAINER_TAG = "z2m" }, "svc.other.service")
    check("tag.code", code, 0)
    check("tag.container_tag", rec.CONTAINER_TAG, "z2m")
end

if failures > 0 then
    os.exit(1)
end
print("PASS  z2m normalize")
