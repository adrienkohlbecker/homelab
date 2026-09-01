-- Unit tests for strip_ansi.lua. Run via `mise run test:fluentbit-lua`
-- or directly with lua5.4.

local here = arg[0]:match("^(.*/)") or "./"
dofile(here .. "strip_ansi.lua")

local function check(label, got, want)
    assert(got == want, string.format("%s: got %s, want %s", label, tostring(got), tostring(want)))
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

-- Cursor moves (erase-line + column-home).
do
    local rec = { log = "\27[2K\27[1Gprogress 50%" }
    strip_ansi("svc.cron.service", 0, rec)
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
