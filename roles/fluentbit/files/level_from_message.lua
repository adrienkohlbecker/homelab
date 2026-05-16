-- podman's --log-driver journald hardcodes PRIORITY by stream: stdout=6
-- (info), stderr=3 (err). Everything from postgres / celery / linuxserver
-- entrypoints arrives at err because the apps write INFO to stderr, which
-- breaks HyperDX severity filtering. Parse the message text for a level
-- keyword and write metadata.otlp.severity_{text,number}, which fluent-bit's
-- opentelemetry output ships as LogRecord.Severity{Text,Number}. The local
-- journal is untouched.
--
-- Scope is deliberately narrow: scan the first 120 chars only, because
-- the level keyword in every format we care about appears right after
-- the timestamp ([INFO], LEVEL:, level=info, etc.) and a later "ERROR"
-- in the body of an info line shouldn't escalate the record. Matches are
-- bordered by non-alphanumerics so "errno" / "warning_count" / "fatalist"
-- don't trigger; padding head with spaces makes the boundary work at the
-- ends of the slice. The borders also stop us false-positive matching
-- path components like /alerts/ -- so we always set a severity (defaulting
-- to info) to keep any downstream body-regex inference from firing on URL
-- noise.
--
-- Reads from record["log"]. An upstream modify filter renames journald's
-- MESSAGE to log, so this is the only field that needs inspecting.
-- Skips records where an upstream filter (flatten_csp.lua) already pinned
-- a severity.

local function has(head, token)
    return string.find(head, "[^%w]" .. token .. "[^%w]") ~= nil
end

function set_priority(tag, ts, group, metadata, record)
    metadata.otlp = metadata.otlp or {}
    if metadata.otlp.severity_text ~= nil then
        -- Upstream filter (flatten_csp.lua for CSP records) already
        -- pinned the severity; leave it alone.
        return 0, ts, metadata, record
    end

    -- An upstream modify filter scoped to nginx.access pre-stamps
    -- record["severity_text"] = "info" so this filter doesn't scan URL
    -- noise for level keywords. Tag-gated to nginx.access: today's
    -- only writer of record["severity_text"] is that modify filter
    -- (CSP records are replaced wholesale by flatten_csp before this
    -- filter runs; systemd journal fields arrive uppercased, so
    -- SEVERITY_TEXT != severity_text). The gate is defence-in-depth:
    -- a future tag-nginx.* input that parses attacker-influenced JSON
    -- (e.g. structured-log nginx tail) would become a forgery channel
    -- without it.
    if tag == "nginx.access" and record["severity_text"] ~= nil then
        metadata.otlp.severity_text = record["severity_text"]
        if record["severity_number"] ~= nil then
            metadata.otlp.severity_number = tonumber(record["severity_number"]) or 0
        end
        return 1, ts, metadata, record
    end

    local msg = record["log"]
    if type(msg) ~= "string" then
        return 0, ts, metadata, record
    end

    local head = " " .. string.lower(string.sub(msg, 1, 120)) .. " "

    local sev_text, sev_num
    if has(head, "fatal") or has(head, "panic") or has(head, "emerg") then
        sev_text, sev_num = "fatal", 21
    elseif has(head, "crit") or has(head, "critical") then
        sev_text, sev_num = "fatal", 21
    elseif has(head, "err") or has(head, "error") then
        sev_text, sev_num = "error", 17
    elseif has(head, "warn") or has(head, "warning") then
        sev_text, sev_num = "warn", 13
    elseif has(head, "notice") then
        -- HyperDX's body-regex maps notice -> warn too; mirror that
        -- so severity stays consistent if we ever turn this filter off.
        sev_text, sev_num = "warn", 13
    elseif has(head, "info") then
        sev_text, sev_num = "info", 9
    elseif string.find(head, "[^%w]log:") ~= nil then
        -- Postgres uses "LOG:" for informational chatter (checkpoints,
        -- autovacuum, etc.). Tightened to require the trailing colon so
        -- the English word "log" anywhere in a message doesn't downgrade
        -- real errors to info.
        sev_text, sev_num = "info", 9
    elseif has(head, "debug") then
        sev_text, sev_num = "debug", 5
    elseif has(head, "trace") then
        sev_text, sev_num = "trace", 1
    else
        -- No level keyword found. Stamp severity=info so HyperDX's
        -- transform processor doesn't fall back to its body-regex
        -- inference and mis-classify e.g. nginx access logs that
        -- happen to contain "alert" / "error" / "warn" as URL path
        -- segments.
        metadata.otlp.severity_text = "info"
        metadata.otlp.severity_number = 9
        return 1, ts, metadata, record
    end

    metadata.otlp.severity_text = sev_text
    metadata.otlp.severity_number = sev_num
    return 1, ts, metadata, record
end
