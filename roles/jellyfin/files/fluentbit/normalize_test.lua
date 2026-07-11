-- Unit tests for Jellyfin's post-JSON Fluent Bit normalizer. The input records
-- model the built-in parser's Preserve_Key On output.

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
    local code = normalize_jellyfin(tag or "svc.jellyfin.service", 0, record)
    return record, code
end

do
    local rec, code = normalize({
        log = '{"@t":"2026-07-11T12:00:00Z","@m":"Loaded plugin: SSO-Auth 4.0.0.4","JellyfinFormat":"clef"}',
        CONTAINER_TAG = "jellyfin",
        CONTAINER_ID = "123456789abc",
        CONTAINER_ID_FULL = "123456789abcdef0",
        CMDLINE = "/usr/bin/conmon --log-tag jellyfin",
        PRIORITY = "6",
        SYSTEMD_UNIT = "jellyfin.service",
        SYSLOG_IDENTIFIER = "jellyfin",
        ["@t"] = "2026-07-11T12:00:00Z",
        ["@m"] = "Loaded plugin: SSO-Auth 4.0.0.4",
        ["@mt"] = "Loaded plugin: {Name} {Version}",
        JellyfinFormat = "clef",
        SourceContext = "Emby.Server.Implementations.Plugins.PluginManager",
        ThreadId = 17,
        Name = "SSO-Auth",
    })
    check("info.code", code, 1)
    check("info.message", rec.log, "Loaded plugin: SSO-Auth 4.0.0.4")
    check("info.level", rec._level, "info")
    check("info.source", rec.source, "Emby.Server.Implementations.Plugins.PluginManager")
    check("info.thread", rec.thread_id, 17)
    check("info.property", rec.Name, "SSO-Auth")
    check("info.status", rec.parser_status, "parsed")
    check("info.timestamp_removed", rec["@t"], nil)
    check("info.template_removed", rec["@mt"], nil)
    check("info.format_removed", rec.JellyfinFormat, nil)
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
        CONTAINER_TAG = "jellyfin",
        ["@t"] = "2026-07-11T12:01:00Z",
        ["@m"] = "Error processing request.",
        ["@l"] = "Error",
        ["@x"] = "System.IO.IOException: Read-only file system\n   at System.IO.Directory.CreateDirectory()",
        ["@tr"] = "trace-1",
        ["@sp"] = "span-1",
        JellyfinFormat = "clef",
    })
    check("error.level", rec._level, "error")
    check(
        "error.exception",
        rec.exception,
        "System.IO.IOException: Read-only file system\n   at System.IO.Directory.CreateDirectory()"
    )
    check("error.trace", rec.trace_id, "trace-1")
    check("error.span", rec.span_id, "span-1")
    check("error.compact_level_removed", rec["@l"], nil)
end

do
    local rec = normalize({
        log = '{"@t":"2026-07-11T12:02:00Z","JellyfinFormat":"clef"}',
        CONTAINER_TAG = "jellyfin",
        ["@t"] = "2026-07-11T12:02:00Z",
        JellyfinFormat = "clef",
    })
    check("schema.status", rec.parser_status, "failed")
    check("schema.error", rec.parse_error, "jellyfin_schema")
    check("schema.level", rec._level, "warn")
    check("schema.raw", rec.log, '{"@t":"2026-07-11T12:02:00Z","JellyfinFormat":"clef"}')
end

do
    local rec = normalize({ log = '{"@t":"broken"', CONTAINER_TAG = "jellyfin" })
    check("json.status", rec.parser_status, "failed")
    check("json.error", rec.parse_error, "jellyfin_json")
    check("json.level", rec._level, "warn")
    check("json.raw", rec.log, '{"@t":"broken"')
end

for label, line in pairs({
    libva = "libva info: va_openDriver() returns 0",
    ffmpeg = "[h264 @ 0x1234] reference picture missing during reorder",
}) do
    local rec = normalize({ log = line, CONTAINER_TAG = "jellyfin" })
    check(label .. ".status", rec.parser_status, "skipped")
    check(label .. ".reason", rec.parser_reason, "non_json")
    check(label .. ".raw", rec.log, line)
    check(label .. ".error", rec.parse_error, nil)
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
print("PASS  jellyfin normalize")
