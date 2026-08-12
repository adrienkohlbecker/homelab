-- Unit tests for the tag-scoped Europe/Paris timestamp conversion.

local here = arg[0]:match("^(.*/)") or "./"
dofile(here .. "europe_paris_time.lua")

local failures = 0

local function check(label, got, want)
    if got ~= want then
        failures = failures + 1
        print(string.format("FAIL  %s\n        got:  %s\n        want: %s", label, tostring(got), tostring(want)))
    end
end

local function converted(field, value)
    local record = { [field] = value }
    local code, timestamp = from_europe_paris("fixture", { sec = 1, nsec = 2 }, record)
    return code, timestamp, record
end

do
    local code, timestamp = converted("plex_timestamp", "Jul 12, 2026 12:00:00.123")
    check("plex.summer.code", code, 1)
    check("plex.summer.seconds", timestamp.sec, 1783850400)
    check("plex.summer.nanoseconds", timestamp.nsec, 123000000)
end

do
    local code, timestamp = converted("nzbtomedia_timestamp", "2026-01-12 12:00:00")
    check("nzbtomedia.winter.code", code, 1)
    check("nzbtomedia.winter.seconds", timestamp.sec, 1768215600)
end

do
    local _, before = converted("tautulli_websocket_timestamp", "2026-03-29 01:59:59")
    local _, after = converted("tautulli_websocket_timestamp", "2026-03-29 03:00:00")
    check("dst.start.before", before.sec, 1774745999)
    check("dst.start.after", after.sec, 1774746000)
end

do
    local _, before = converted("tautulli_websocket_timestamp", "2026-10-25 01:59:59")
    local _, after = converted("tautulli_websocket_timestamp", "2026-10-25 03:00:00")
    check("dst.end.before", before.sec, 1792886399)
    check("dst.end.after", after.sec, 1792893600)
end

do
    local original = { sec = 10, nsec = 20 }
    local code, timestamp = from_europe_paris("fixture", original, { message = "unrelated" })
    check("unrelated.code", code, 0)
    check("unrelated.timestamp", timestamp, original)
end

do
    local code, timestamp, record = from_europe_paris(
        "fixture",
        { sec = 10, nsec = 20 },
        { nzbtomedia_timestamp = "2026-02-30 12:00:00" }
    )
    check("invalid.code", code, 2)
    check("invalid.timestamp.seconds", timestamp.sec, 10)
    check("invalid.parse_error", record.parse_error, "europe_paris_time")
    check("invalid.level", record._level, "warn")
end

if failures > 0 then
    os.exit(1)
end
print("europe_paris_time: all assertions passed")
