local here = arg[0]:match("^(.*/)") or "./"
dofile(here .. "normalize.lua")

local failures = 0

local function check(label, actual, expected)
    if actual ~= expected then
        failures = failures + 1
        print(string.format("FAIL %s: expected %s, got %s", label, tostring(expected), tostring(actual)))
    end
end

local function parsed_record(level, message, source)
    return {
        log = "raw console line\n",
        CONTAINER_TAG = "homepage",
        CONTAINER_ID = "98247da5414a",
        CONTAINER_ID_FULL = "98247da5414a53ee",
        CMDLINE = "/usr/bin/conmon --log-tag homepage",
        PRIORITY = "6",
        SYSTEMD_UNIT = "homepage.service",
        SYSLOG_IDENTIFIER = "homepage",
        homepage_timestamp = "2026-07-11T21:00:00.000Z",
        homepage_level = level,
        homepage_source = source,
        homepage_message = message,
    }
end

do
    local code, _, rec = normalize_homepage(
        "svc.homepage.service",
        1,
        parsed_record("info", "Widget request completed\n", "service-helpers")
    )
    check("info.code", code, 1)
    check("info.message", rec.log, "Widget request completed")
    check("info.level", rec._level, "info")
    check("info.source", rec.source, "service-helpers")
    check("info.status", rec.parser_status, "parsed")
    check("info.timestamp_removed", rec.homepage_timestamp, nil)
    check("info.source_field_removed", rec.homepage_source, nil)
    check("info.short_container_kept", rec.CONTAINER_ID, "98247da5414a")
    check("info.container_tag_removed", rec.CONTAINER_TAG, nil)
    check("info.full_container_removed", rec.CONTAINER_ID_FULL, nil)
    check("info.command_removed", rec.CMDLINE, nil)
    check("info.priority_removed", rec.PRIORITY, nil)
    check("info.unit_removed", rec.SYSTEMD_UNIT, nil)
    check("info.identifier_removed", rec.SYSLOG_IDENTIFIER, nil)
end

for source_level, level in pairs({
    error = "error",
    warn = "warn",
    info = "info",
    http = "info",
    verbose = "debug",
    debug = "debug",
    silly = "trace",
}) do
    local _, _, rec = normalize_homepage("svc.homepage.service", 1, parsed_record(source_level, "Level fixture", nil))
    check("level." .. source_level, rec._level, level)
    check("level." .. source_level .. ".source", rec.source, nil)
end

do
    local _, _, rec = normalize_homepage(
        "svc.homepage.service",
        1,
        parsed_record(
            "error",
            "Error: widget failed\n    at verify (/app/verify.js:7:1)\n    at async handler (/app/api.js:9:2)\n",
            "homepage.verify"
        )
    )
    check(
        "stack.message",
        rec.log,
        "Error: widget failed\n    at verify (/app/verify.js:7:1)\n    at async handler (/app/api.js:9:2)"
    )
    check("stack.level", rec._level, "error")
    check("stack.source", rec.source, "homepage.verify")
end

do
    local _, _, rec = normalize_homepage("svc.homepage.service", 1, {
        log = "[2026-07-11T21:00:00.000Z] malformed Homepage header\n",
        CONTAINER_TAG = "homepage",
    })
    check("malformed.status", rec.parser_status, "failed")
    check("malformed.error", rec.parse_error, "homepage_winston")
    check("malformed.level", rec._level, "warn")
    check("malformed.raw", rec.log, "[2026-07-11T21:00:00.000Z] malformed Homepage header")
end

for label, line in pairs({
    entrypoint = "Skipping ownership changes for /app/config\n",
    nextjs = "▲ Next.js 16.2.6\n",
    ready = "✓ Ready in 0ms\n",
}) do
    local _, _, rec = normalize_homepage("svc.homepage.service", 1, {
        log = line,
        CONTAINER_TAG = "homepage",
        PRIORITY = "6",
    })
    check(label .. ".message", rec.log, line:gsub("[\r\n]+$", ""))
    check(label .. ".status", rec.parser_status, "skipped")
    check(label .. ".reason", rec.parser_reason, "non_winston")
    check(label .. ".priority_removed", rec.PRIORITY, nil)
end

do
    local raw = "[2026-07-11T21:00:00.000Z] info: <systemd> untouched"
    local rec = {
        log = raw,
        SYSTEMD_UNIT = "homepage.service",
        homepage_timestamp = "2026-07-11T21:00:00.000Z",
        homepage_level = "info",
        homepage_source = "systemd",
        homepage_message = "untouched",
    }
    local code = normalize_homepage("svc.homepage.service", 1, rec)
    check("unit.code", code, 1)
    check("unit.raw", rec.log, raw)
    check("unit.unit_kept", rec.SYSTEMD_UNIT, "homepage.service")
    check("unit.timestamp_removed", rec.homepage_timestamp, nil)
    check("unit.level_removed", rec.homepage_level, nil)
    check("unit.source_removed", rec.homepage_source, nil)
    check("unit.message_removed", rec.homepage_message, nil)
end

do
    local rec = { log = "untouched", CONTAINER_TAG = "homepage" }
    local code = normalize_homepage("svc.other.service", 1, rec)
    check("tag.code", code, 0)
    check("tag.container_kept", rec.CONTAINER_TAG, "homepage")
end

if failures > 0 then
    os.exit(1)
end

print("homepage normalize: ok")
