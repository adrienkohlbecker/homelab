-- podman's --log-driver journald hardcodes PRIORITY by stream: stdout=6
-- (info), stderr=3 (err). Everything from postgres / celery / linuxserver
-- entrypoints arrives at err because the apps write INFO to stderr, which
-- breaks `journalctl -p` and OpenObserve/HyperDX severity filtering.
-- This filter parses the message text for a level token and overrides
-- both the journald-style PRIORITY/level (consumed by OpenObserve) and
-- the OTLP-side severity_text/severity_number (consumed by fluent-bit's
-- opentelemetry output via logs_severity_*_message_key, mapped to
-- LogRecord.Severity in HyperDX). The local journal is untouched.
--
-- Scope is deliberately narrow: scan the first 120 chars only, because
-- the level keyword in every format we care about appears right after
-- the timestamp ([INFO], LEVEL:, level=info, etc.) and a later "ERROR"
-- in the body of an info line shouldn't escalate the record. Matches are
-- bordered by non-alphanumerics so "errno" / "warning_count" / "fatalist"
-- don't trigger; padding head with spaces makes the boundary work at the
-- ends of the slice. Importantly the borders also stop us false-positive
-- matching path components like /alerts/ (HyperDX's transform processor
-- DOES regex-match those on Body via \b -- so we always set
-- severity_text/severity_number from this filter, including a default
-- "info" pair when no keyword is found, to keep the downstream
-- inference from firing on URLs that happen to look like a level word).
--
-- Also reads from record["log"] (the tail input's content) when MESSAGE
-- isn't present so nginx access/error tails get severity stamped too.
-- Skips records where an upstream filter (flatten-csp.lua) already pinned
-- a severity.

local function has(head, token)
    return string.find(head, "[^%w]" .. token .. "[^%w]") ~= nil
end

function set_priority(tag, ts, record)
    if record["severity_text"] ~= nil then
        -- Upstream filter (flatten-csp.lua for CSP records) already
        -- pinned the severity; leave it alone.
        return 0, ts, record
    end

    local msg = record["MESSAGE"] or record["log"]
    if type(msg) ~= "string" then
        -- No body to inspect; let HyperDX's transform processor do
        -- what it can.
        return 0, ts, record
    end

    local head = " " .. string.lower(string.sub(msg, 1, 120)) .. " "

    local prio, level, sev_text, sev_num
    if has(head, "fatal") or has(head, "panic") or has(head, "emerg") then
        prio, level, sev_text, sev_num = "2", "fatal", "fatal", 21
    elseif has(head, "crit") or has(head, "critical") then
        prio, level, sev_text, sev_num = "2", "critical", "fatal", 21
    elseif has(head, "err") or has(head, "error") then
        prio, level, sev_text, sev_num = "3", "error", "error", 17
    elseif has(head, "warn") or has(head, "warning") or has(head, "wrn") then
        prio, level, sev_text, sev_num = "4", "warning", "warn", 13
    elseif has(head, "notice") then
        -- HyperDX's body-regex maps notice -> warn too; mirror that
        -- so severity stays consistent if we ever turn this filter off.
        prio, level, sev_text, sev_num = "5", "notice", "warn", 13
    elseif has(head, "info") or has(head, "inf") then
        prio, level, sev_text, sev_num = "6", "info", "info", 9
    elseif string.find(head, "[^%w]log:") ~= nil then
        -- Postgres uses "LOG:" for informational chatter (checkpoints,
        -- autovacuum, etc.). Tightened to require the trailing colon so
        -- the English word "log" anywhere in a message doesn't downgrade
        -- real errors to info.
        prio, level, sev_text, sev_num = "6", "info", "info", 9
    elseif has(head, "debug") or has(head, "dbg") then
        prio, level, sev_text, sev_num = "7", "debug", "debug", 5
    elseif has(head, "trace") then
        prio, level, sev_text, sev_num = "7", "trace", "trace", 1
    else
        -- No level keyword found. Stamp severity=info so HyperDX's
        -- transform processor doesn't fall back to its body-regex
        -- inference and mis-classify e.g. nginx access logs that
        -- happen to contain "alert" / "error" / "warn" as URL path
        -- segments. PRIORITY and level are left as the upstream
        -- input supplied (journald sets PRIORITY for journal records;
        -- tail input doesn't set it for nginx tails).
        record["severity_text"] = "info"
        record["severity_number"] = 9
        return 1, ts, record
    end

    record["PRIORITY"] = prio
    record["level"] = level
    record["severity_text"] = sev_text
    record["severity_number"] = sev_num
    return 2, ts, record
end
