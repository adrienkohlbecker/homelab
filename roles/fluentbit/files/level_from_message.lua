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
-- Skips records where an upstream filter already pinned a severity.

local function has(head, token)
    return string.find(head, "[^%w]" .. token .. "[^%w]") ~= nil
end

-- Numeric mapping (OTLP SEVERITY_NUMBER_*); rules below pick a
-- severity text, this table converts to the number.
local SEV = {
    fatal = 21,
    error = 17,
    warn = 13,
    info = 9,
    debug = 5,
    trace = 1,
}

-- Priority-ordered: first match wins. Higher-severity keywords come
-- first so a warning in the body of a fatal record doesn't downgrade.
-- "notice" maps to info, not warn: dnscrypt-proxy emits routine latency
-- probe + server-selection output at [NOTICE] (~hundreds of lines/day),
-- which is operationally info -- not a warning. This deviates from
-- HyperDX's body-regex (which maps notice -> warn); the lua filter
-- stamps severity before the body-regex runs, so the stamp wins.
-- Postgres emits "LOG: ..." for informational chatter -- the pattern
-- requires a trailing colon so the English word "log" anywhere in a
-- real error doesn't downgrade it, and LOG: ranks after info so a
-- "LOG: ... error" record still classifies as error.
local LEVEL_RULES = {
    { keywords = { "fatal", "panic", "emerg", "crit", "critical" }, text = "fatal" },
    { keywords = { "err", "error" }, text = "error" },
    { keywords = { "warn", "warning" }, text = "warn" },
    { keywords = { "info", "notice" }, text = "info" },
    { pattern = "[^%w]log:", text = "info" },
    { keywords = { "debug" }, text = "debug" },
    { keywords = { "trace" }, text = "trace" },
}

local function match_rule(head, rule)
    if rule.keywords then
        for _, kw in ipairs(rule.keywords) do
            if has(head, kw) then
                return true
            end
        end
        return false
    end
    return string.find(head, rule.pattern) ~= nil
end

function set_priority(tag, ts, _group, metadata, record)
    metadata.otlp = metadata.otlp or {}
    if metadata.otlp.severity_text ~= nil then
        -- An upstream filter already pinned the severity; leave it alone.
        return 0, ts, metadata, record
    end

    -- An upstream modify filter scoped to nginx.access pre-stamps
    -- record["severity_text"] = "info" so this filter doesn't scan URL
    -- noise for level keywords. Tag-gated to nginx.access: today's
    -- only writer of record["severity_text"] is that modify filter
    -- (systemd journal fields arrive uppercased, so SEVERITY_TEXT !=
    -- severity_text). The gate is defence-in-depth:
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

    local sev_text
    for _, rule in ipairs(LEVEL_RULES) do
        if match_rule(head, rule) then
            sev_text = rule.text
            break
        end
    end

    if sev_text == nil then
        -- No level keyword found. Stamp severity=info so HyperDX's
        -- transform processor doesn't fall back to its body-regex
        -- inference and mis-classify e.g. nginx access logs that
        -- happen to contain "alert" / "error" / "warn" as URL path
        -- segments.
        sev_text = "info"
    end

    metadata.otlp.severity_text = sev_text
    metadata.otlp.severity_number = SEV[sev_text]
    return 1, ts, metadata, record
end
