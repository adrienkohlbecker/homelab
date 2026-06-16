-- podman's --log-driver journald hardcodes PRIORITY by stream: stdout=6
-- (info), stderr=3 (err). Everything from postgres / celery / linuxserver
-- entrypoints arrives at err because the apps write INFO to stderr, which
-- loses useful severity filtering. Parse the message text for a level keyword
-- and write the inferred text level into the temp record field _level; the
-- final lnav shaper reads it, promotes it into the JSONL record body (level),
-- and drops the temp field. The local journal is untouched.
--
-- This and lnav_shape use the classic 3-argument lua-filter signature
-- (tag, timestamp, record). Fluent Bit auto-selects the metadata-aware
-- 5-argument prototype when the function declares group/metadata parameters,
-- and then serializes the returned event metadata into the output as an
-- `__internal__` object -- so the level is threaded through a record field
-- rather than the metadata channel to keep that object out of the JSONL.
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

-- Priority-ordered: first match wins. Higher-severity keywords come
-- first so a warning in the body of a fatal record doesn't downgrade.
-- "notice" maps to info, not warn: dnscrypt-proxy emits routine latency
-- probe + server-selection output at [NOTICE] (~hundreds of lines/day),
-- which is operationally info -- not a warning. This deviates from
-- the older body-regex mapping, which treated notice as warn.
-- Postgres emits "LOG: ..." for informational chatter -- the pattern
-- requires a trailing colon so the English word "log" anywhere in a
-- real error doesn't downgrade it, and LOG: ranks after info so a
-- "LOG: ... error" record still classifies as error.
-- The 3-letter tokens (inf/wrn/ftl/dbg/trc/pnc) are zerolog's abbreviated
-- console levels, which Go services like headscale emit right after the
-- timestamp ("2026-... WRN Listening without TLS ..."). ERR already matched
-- via "err"; without their own tokens WRN/FTL/PNC would miss every rule and
-- fall through to the info default. The [^%w] borders keep them from firing
-- inside longer words -- "info"/"information" never trip the bare "inf".
local LEVEL_RULES = {
    { keywords = { "fatal", "panic", "emerg", "crit", "critical", "ftl", "pnc" }, text = "fatal" },
    { keywords = { "err", "error" }, text = "error" },
    { keywords = { "warn", "warning", "wrn" }, text = "warn" },
    { keywords = { "info", "notice", "inf" }, text = "info" },
    { pattern = "[^%w]log:", text = "info" },
    { keywords = { "debug", "dbg" }, text = "debug" },
    { keywords = { "trace", "trc" }, text = "trace" },
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

function set_priority(tag, ts, record)
    if record["_level"] ~= nil then
        -- An upstream filter already pinned the level; leave it alone.
        return 0, ts, record
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
        record["_level"] = record["severity_text"]
        return 1, ts, record
    end

    local msg = record["log"]
    if type(msg) ~= "string" then
        return 0, ts, record
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
        -- No level keyword found. Stamp level=info so the final
        -- JSONL record still has a predictable lnav level.
        sev_text = "info"
    end

    record["_level"] = sev_text
    return 1, ts, record
end
