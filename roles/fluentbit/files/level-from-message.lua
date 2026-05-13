-- podman's --log-driver journald hardcodes PRIORITY by stream: stdout=6
-- (info), stderr=3 (err). Everything from postgres / celery / linuxserver
-- entrypoints arrives at err because the apps write INFO to stderr, which
-- breaks `journalctl -p` and the OpenObserve priority filter. This filter
-- parses the message text for a level token and overrides PRIORITY before
-- shipping. The local journal is untouched; only the OpenObserve copy
-- gets the corrected priority.
--
-- Scope is deliberately narrow: scan the first 120 chars only, because
-- the level keyword in every format we care about appears right after
-- the timestamp ([INFO], LEVEL:, level=info, etc.) and a later "ERROR"
-- in the body of an info line shouldn't escalate the record. Matches are
-- bordered by non-alphanumerics so "errno" / "warning_count" / "fatalist"
-- don't trigger; padding head with spaces makes the boundary work at the
-- ends of the slice.

local function has(head, token)
    return string.find(head, "[^%w]" .. token .. "[^%w]") ~= nil
end

function set_priority(tag, ts, record)
    local msg = record["MESSAGE"]
    if type(msg) ~= "string" then return 0, ts, record end

    local head = " " .. string.lower(string.sub(msg, 1, 120)) .. " "

    local prio, level
    if has(head, "fatal") or has(head, "panic") or has(head, "emerg") then
        prio, level = "2", "fatal"
    elseif has(head, "crit") or has(head, "critical") then
        prio, level = "2", "critical"
    elseif has(head, "err") or has(head, "error") then
        prio, level = "3", "error"
    elseif has(head, "warn") or has(head, "warning") or has(head, "wrn") then
        prio, level = "4", "warning"
    elseif has(head, "notice") then
        prio, level = "5", "notice"
    elseif has(head, "info") or has(head, "inf") then
        prio, level = "6", "info"
    elseif string.find(head, "[^%w]log:") ~= nil then
        -- Postgres uses "LOG:" for informational chatter (checkpoints,
        -- autovacuum, etc.). Tightened to require the trailing colon so
        -- the English word "log" anywhere in a message doesn't downgrade
        -- real errors to info.
        prio, level = "6", "info"
    elseif has(head, "debug") or has(head, "dbg") or has(head, "trace") then
        prio, level = "7", "debug"
    else
        return 0, ts, record
    end

    record["PRIORITY"] = prio
    record["level"] = level
    return 2, ts, record
end
