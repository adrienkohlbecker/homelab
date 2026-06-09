-- Unit tests for strip_ansi.lua. Run via `mise run test:fluentbit-lua`
-- or directly with lua5.4.

local here = arg[0]:match("^(.*/)") or "./"
dofile(here .. "strip_ansi.lua")

local failures = 0

local function check(label, got, want)
    if got ~= want then
        failures = failures + 1
        print(string.format("FAIL  %s\n        got:  %s\n        want: %s", label, tostring(got), tostring(want)))
    end
end

-- seerr (jellyseerr) wraps its level token in SGR colour codes.
do
    local rec = { log = "2026-06-09T20:21:00.016Z [\27[34mdebug\27[39m][Jobs]: Starting" }
    local code = strip_ansi("svc.seerr.service", 0, rec)
    check("seerr.clean", rec.log, "2026-06-09T20:21:00.016Z [debug][Jobs]: Starting")
    check("seerr.code", code, 1)
end

-- Multiple/compound SGR params.
do
    local rec = { log = "\27[1;32mOK\27[0m done" }
    strip_ansi("svc.x.service", 0, rec)
    check("sgr.compound", rec.log, "OK done")
end

-- Cursor moves (pihole gravity cron style): erase-line + column-home.
do
    local rec = { log = "\27[2K\27[1Gprogress 50%" }
    strip_ansi("pihole_gravity", 0, rec)
    check("cursor.clean", rec.log, "progress 50%")
end

-- No escapes: untouched, return 0 so the record is not re-stamped.
do
    local rec = { log = "plain message, no colour" }
    local code = strip_ansi("svc.x.service", 0, rec)
    check("plain.unchanged", rec.log, "plain message, no colour")
    check("plain.code", code, 0)
end

-- Non-string body (already-structured record): return 0, leave it alone.
do
    local rec = { log = 42 }
    local code = strip_ansi("svc.x.service", 0, rec)
    check("nonstring.code", code, 0)
end

if failures == 0 then
    print("strip_ansi: all assertions passed")
    os.exit(0)
else
    print(string.format("strip_ansi: %d assertion(s) failed", failures))
    os.exit(1)
end
