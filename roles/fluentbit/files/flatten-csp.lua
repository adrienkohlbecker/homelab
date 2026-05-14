-- Flatten browser CSP violation reports (legacy report-uri + modern
-- Reporting API) into a uniform set of top-level csp_* keys so HyperDX
-- can filter and group on them via LogAttributes['csp_*'] directly,
-- without JSONExtract on a nested JSON-stringified blob. Also synthesises
-- a one-line `log` body for the HyperDX timeline view.
--
-- Legacy report-uri body  : {"csp-report": {"blocked-uri": "...", ...}}
-- Reporting API body      : [{"type":"csp-violation","body":{"blockedURL":"..."}, ...}]
-- The Reporting API ships a JSON array; fluent-bit's http input splits it
-- into one record per array element, so this filter only sees the
-- per-violation shape regardless of which protocol the browser used.
--
-- Keys differ between the two formats (kebab-case vs camelCase) and the
-- "violated" / "effective" split was renamed mid-spec, so each csp_* key
-- below tries all known aliases and falls back to the other-direction
-- alias to keep the column populated on old Chromium and modern Firefox
-- alike.

local function pick(t, ...)
    if type(t) ~= "table" then return nil end
    for _, k in ipairs({ ... }) do
        local v = t[k]
        if v ~= nil and v ~= "" then return v end
    end
    return nil
end

function flatten_csp(tag, ts, record)
    -- SYSLOG_IDENTIFIER + a default body are set unconditionally so
    -- unparseable POSTs (malformed JSON, future browser format we don't
    -- know yet) still land in HyperDX under the csplogger filter rather
    -- than disappearing into the otel_logs catch-all.
    record["SYSLOG_IDENTIFIER"] = "csplogger"
    if record["log"] == nil then
        record["log"] = "csp_violation"
    end

    local r, format
    if type(record["csp-report"]) == "table" then
        r = record["csp-report"]
        format = "legacy"
        record["csp-report"] = nil
    elseif record["type"] == "csp-violation" and type(record["body"]) == "table" then
        r = record["body"]
        format = "reporting-api"
        -- Promote the Reporting API envelope fields (only present in
        -- this format) before dropping the originals.
        record["csp_age"] = record["age"]
        record["csp_url"] = record["url"]
        record["csp_user_agent"] = record["user_agent"]
        record["body"] = nil
        record["type"] = nil
        record["age"] = nil
        record["url"] = nil
        record["user_agent"] = nil
    else
        -- Unrecognised shape; keep the SYSLOG_IDENTIFIER + default body
        -- and pass the record through untouched for diagnosis.
        return 1, ts, record
    end

    record["csp_format"]              = format
    record["csp_blocked_uri"]         = pick(r, "blocked-uri", "blockedURL")
    record["csp_document_uri"]        = pick(r, "document-uri", "documentURL")
    record["csp_violated_directive"]  = pick(r, "violated-directive", "violatedDirective",
                                                "effective-directive", "effectiveDirective")
    record["csp_effective_directive"] = pick(r, "effective-directive", "effectiveDirective",
                                                "violated-directive", "violatedDirective")
    record["csp_original_policy"]     = pick(r, "original-policy", "originalPolicy")
    record["csp_disposition"]         = pick(r, "disposition")
    record["csp_referrer"]            = pick(r, "referrer")
    record["csp_source_file"]         = pick(r, "source-file", "sourceFile")
    record["csp_sample"]              = pick(r, "script-sample", "sample")
    record["csp_line_number"]         = pick(r, "line-number", "lineNumber")
    record["csp_column_number"]       = pick(r, "column-number", "columnNumber")
    record["csp_status_code"]         = pick(r, "status-code", "statusCode")

    local directive = record["csp_effective_directive"] or "?"
    local blocked = record["csp_blocked_uri"] or "?"
    local document = record["csp_document_uri"] or "?"
    record["log"] = string.format("CSP %s blocked %s on %s", directive, blocked, document)

    return 2, ts, record
end
