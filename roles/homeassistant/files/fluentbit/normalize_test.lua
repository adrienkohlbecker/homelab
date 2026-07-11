-- Unit tests for Home Assistant's post-regex Fluent Bit normalizer. Input
-- records model the parser filter's Reserve_Data/Preserve_Key output.

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
    local code = normalize_homeassistant(tag or "svc.homeassistant.service", 0, record)
    return record, code
end

do
    local rec, code = normalize({
        log = "2026-07-11 18:26:42.804 WARNING (MainThread) [homeassistant.helpers.service] Referenced entity is unavailable\n",
        CONTAINER_TAG = "homeassistant",
        CONTAINER_ID = "123456789abc",
        CONTAINER_ID_FULL = "123456789abcdef0",
        CMDLINE = "/usr/bin/conmon --log-tag homeassistant",
        PRIORITY = "3",
        SYSTEMD_UNIT = "homeassistant.service",
        SYSLOG_IDENTIFIER = "homeassistant",
        ha_timestamp = "2026-07-11 18:26:42.804",
        ha_level = "WARNING",
        ha_thread = "MainThread",
        ha_source = "homeassistant.helpers.service",
        ha_message = "Referenced entity is unavailable\n",
    })
    check("warning.code", code, 1)
    check("warning.message", rec.log, "Referenced entity is unavailable")
    check("warning.level", rec._level, "warn")
    check("warning.thread", rec.thread, "MainThread")
    check("warning.source", rec.source, "homeassistant.helpers.service")
    check("warning.status", rec.parser_status, "parsed")
    check("warning.short_container_id_kept", rec.CONTAINER_ID, "123456789abc")
    check("warning.timestamp_removed", rec.ha_timestamp, nil)
    check("warning.container_tag_removed", rec.CONTAINER_TAG, nil)
    check("warning.full_container_id_removed", rec.CONTAINER_ID_FULL, nil)
    check("warning.cmdline_removed", rec.CMDLINE, nil)
    check("warning.priority_removed", rec.PRIORITY, nil)
    check("warning.unit_removed", rec.SYSTEMD_UNIT, nil)
    check("warning.identifier_removed", rec.SYSLOG_IDENTIFIER, nil)
end

do
    local rec = normalize({
        log = "2026-07-11 18:27:00.000 ERROR (SyncWorker_0) [aiodhcpwatcher] Operation not permitted",
        CONTAINER_TAG = "homeassistant",
        ha_timestamp = "2026-07-11 18:27:00.000",
        ha_level = "ERROR",
        ha_thread = "SyncWorker_0",
        ha_source = "aiodhcpwatcher",
        ha_message = 'Operation not permitted\nTraceback (most recent call last):\n  File "watcher.py", line 7\nPermissionError',
    })
    check("error.level", rec._level, "error")
    check("error.thread", rec.thread, "SyncWorker_0")
    check("error.source", rec.source, "aiodhcpwatcher")
    check(
        "error.message",
        rec.log,
        'Operation not permitted\nTraceback (most recent call last):\n  File "watcher.py", line 7\nPermissionError'
    )
end

do
    local rec = normalize({
        log = 'Traceback (most recent call last):\n  File "worker.py", line 7, in run\nValueError: bad input\n',
        CONTAINER_TAG = "homeassistant",
        PRIORITY = "3",
    })
    check(
        "continuation.message",
        rec.log,
        'Traceback (most recent call last):\n  File "worker.py", line 7, in run\nValueError: bad input'
    )
    check("continuation.status", rec.parser_status, "skipped")
    check("continuation.reason", rec.parser_reason, "continuation")
    check("continuation.error", rec.parse_error, nil)
    check("continuation.priority_removed", rec.PRIORITY, nil)
end

do
    local rec = normalize({
        log = "2026-07-11 malformed Home Assistant header",
        CONTAINER_TAG = "homeassistant",
    })
    check("malformed.status", rec.parser_status, "failed")
    check("malformed.error", rec.parse_error, "homeassistant_text")
    check("malformed.level", rec._level, "warn")
    check("malformed.raw", rec.log, "2026-07-11 malformed Home Assistant header")
end

do
    local rec, code = normalize({ log = "podman command output" })
    check("unit_output.code", code, 0)
    check("unit_output.status", rec.parser_status, nil)
    check("unit_output.raw", rec.log, "podman command output")
end

do
    local rec, code = normalize({ log = "untouched", CONTAINER_TAG = "other" }, "svc.other.service")
    check("tag.code", code, 0)
    check("tag.status", rec.parser_status, nil)
end

if failures > 0 then
    os.exit(1)
end
print("PASS  homeassistant normalize")
