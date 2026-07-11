local here = arg[0]:match("^(.*/)") or "./"
dofile(here .. "normalize.lua")

local failures = 0

local function check(label, actual, expected)
    if actual ~= expected then
        failures = failures + 1
        print(string.format("FAIL %s: expected %s, got %s", label, tostring(expected), tostring(actual)))
    end
end

local function parsed_record(level, message)
    return {
        log = "raw console line\n",
        CONTAINER_TAG = "bazarr",
        CONTAINER_ID = "98247da5414a",
        CONTAINER_ID_FULL = "98247da5414a53ee",
        PRIORITY = "3",
        SYSTEMD_UNIT = "bazarr.service",
        bazarr_time = "2026-07-11 21:18:53,475",
        bazarr_level = level,
        logger = "root",
        thread_id = "7af21645eb30",
        source = "movies",
        source_line = 131,
        message = message,
    }
end

local code, _, rec = normalize_bazarr(
    "svc.bazarr.service",
    1,
    parsed_record("INFO", "BAZARR Finished searching for missing Movies Subtitles.\n")
)
check("info.code", code, 1)
check("info.message", rec.log, "BAZARR Finished searching for missing Movies Subtitles.")
check("info.level", rec._level, "info")
check("info.status", rec.parser_status, "parsed")
check("info.logger", rec.logger, "root")
check("info.thread", rec.thread_id, "7af21645eb30")
check("info.source", rec.source, "movies")
check("info.source_line", rec.source_line, 131)
check("info.timestamp_removed", rec.bazarr_time, nil)
check("info.severity_removed", rec.bazarr_level, nil)
check("info.container_tag_removed", rec.CONTAINER_TAG, nil)
check("info.priority_removed", rec.PRIORITY, nil)
check("info.short_container_kept", rec.CONTAINER_ID, "98247da5414a")

_, _, rec = normalize_bazarr(
    "svc.bazarr.service",
    1,
    parsed_record("WARNING", "HTTPSConnectionPool(host='api.gestdown.info'): Read timed out.\n")
)
check("warning.level", rec._level, "warn")
check("warning.message", rec.log, "HTTPSConnectionPool(host='api.gestdown.info'): Read timed out.")

_, _, rec = normalize_bazarr("svc.bazarr.service", 1, parsed_record("ERROR", "Provider failed\n"))
check("error.level", rec._level, "error")

_, _, rec = normalize_bazarr("svc.bazarr.service", 1, parsed_record("NOTICE", "Unknown level\n"))
check("level.status", rec.parser_status, "failed")
check("level.error", rec.parse_error, "bazarr_level")
check("level.level", rec._level, "warn")

local malformed = {
    log = "2026-07-11 21:18:53,475 - format drift\n",
    CONTAINER_TAG = "bazarr",
    PRIORITY = "3",
}
_, _, rec = normalize_bazarr("svc.bazarr.service", 1, malformed)
check("malformed.status", rec.parser_status, "failed")
check("malformed.error", rec.parse_error, "bazarr_console")
check("malformed.raw", rec.log, "2026-07-11 21:18:53,475 - format drift")

local prose = {
    log = "Linuxserver.io version: v1.5.2-ls318\n",
    CONTAINER_TAG = "bazarr",
    PRIORITY = "6",
}
_, _, rec = normalize_bazarr("svc.bazarr.service", 1, prose)
check("prose.status", rec.parser_status, "skipped")
check("prose.reason", rec.parser_reason, "non_bazarr")
check("prose.message", rec.log, "Linuxserver.io version: v1.5.2-ls318")
check("prose.priority_removed", rec.PRIORITY, nil)

local unit_record = { log = "Starting bazarr...", SYSTEMD_UNIT = "bazarr.service" }
code, _, rec = normalize_bazarr("svc.bazarr.service", 1, unit_record)
check("unit.code", code, 0)
check("unit.message", rec.log, "Starting bazarr...")
check("unit.systemd_unit", rec.SYSTEMD_UNIT, "bazarr.service")

local wrong_tag = { log = "message", CONTAINER_TAG = "bazarr" }
code, _, rec = normalize_bazarr("svc.other.service", 1, wrong_tag)
check("tag.code", code, 0)
check("tag.container_tag", rec.CONTAINER_TAG, "bazarr")

if failures > 0 then
    os.exit(1)
end

print("bazarr normalize: ok")
