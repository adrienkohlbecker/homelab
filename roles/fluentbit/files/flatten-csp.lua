-- Flatten browser CSP violation reports (legacy report-uri + modern
-- Reporting API) into one LogRecord per violation with all the
-- structured fields landed as LogAttributes on the HyperDX side.
--
-- Wire path: fluent-bit OTLP HTTP output -> hyperdx otelcontribcol's
-- otlp/hyperdx receiver -> transform processor -> ClickHouse otel_logs.
-- Severity is shipped directly: we set record["severity_text"] /
-- record["severity_number"] at the body top level, and the [OUTPUT]
-- opentelemetry block's logs_severity_*_message_key options point
-- fluent-bit at those body keys (the default $SeverityText reads from
-- fluent-bit's separate event-metadata stream, which lua filters have
-- no API to populate).
--
-- LogAttributes don't have a body-side message_key option, so we lean
-- on HyperDX's transform processor: it runs
-- ExtractPatterns("(?P<0>(\\{.*\\}))") on log.body + ParseJSON +
-- merge_maps into log.attributes unconditionally, so any JSON object
-- substring in Body becomes flat LogAttributes (this is the same path
-- that already populates z2m's MQTT-payload bodies as
-- LogAttributes['linkquality'] etc.).
--
-- Body has shape:
--   CSP <effective-directive> blocked <blocked-uri> on <document-uri> {<csp_json>}
--
-- where <csp_json> contains csp_format, host, and the populated csp_*
-- fields. Service identity is carried in ResourceAttributes (set by
-- otlp-shape.lua); no need to duplicate it as a LogAttribute via the
-- old journald-style SYSLOG_IDENTIFIER trick.
--
-- Legacy report-uri body  : {"csp-report": {"blocked-uri": "...", ...}}
-- Reporting API body      : [{"type":"csp-violation","body":{"blockedURL":"..."}, ...}]
-- fluent-bit's http input splits the array, so this filter only sees
-- the per-violation shape regardless of which protocol the browser used.

local function pick(t, ...)
    if type(t) ~= "table" then return nil end
    for _, k in ipairs({ ... }) do
        local v = t[k]
        if v ~= nil and v ~= "" then return v end
    end
    return nil
end

-- Per-field truncation bound for the JSON suffix that lands as
-- LogAttributes downstream. CSP report fields are tiny in real
-- traffic (URIs, directive names, short policy strings); the bound
-- exists to cap attacker-controlled fields (script-sample,
-- blocked-uri) that could otherwise bloat Body up to nginx's 64KB
-- request limit.
local MAX_FIELD_BYTES = 1024

-- JSON string escaper for the LogAttributes-as-suffix encoding.
-- Defends three classes of input:
--   1. Control bytes (\x00-\x1F + DEL) -- browsers occasionally
--      include tab/newline in script-sample for multi-line inline
--      scripts; downstream readers handle them inconsistently and
--      they can smuggle log-format separators (CRLF, ANSI escapes).
--   2. Non-ASCII bytes (\x80-\xFF) -- dropped wholesale to neutralise
--      Unicode bidi-override attacks (U+202A-U+202E and U+2066-U+2069
--      in UTF-8) that can make a row's apparent text read backwards
--      in any consumer that renders Body as Unicode. Legitimate non-
--      ASCII content in CSP report fields is vanishingly rare (URIs
--      are already percent-encoded, directive names are ASCII).
--   3. HTML breakouts via forward slash -- escaping "/" as "\/" is
--      legal JSON and defeats "</script>" sequences in attacker-
--      supplied URIs if anything downstream renders Body in an HTML
--      context (HyperDX log detail pane, future dashboards). JSON
--      doesn't require this; it's a defence-in-depth measure.
-- After the strip + escape we cap the result at MAX_FIELD_BYTES so
-- a hostile poster can't bloat Body by stuffing one field with 64KB
-- of escaped-but-legal content.
local function jsonesc(s)
    if s == nil then return "" end
    s = tostring(s)
    s = s:gsub("[%c\127-\255]", "")
    s = s:gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("/", "\\/")
    if #s > MAX_FIELD_BYTES then s = s:sub(1, MAX_FIELD_BYTES) end
    return s
end

-- Strip query string and fragment from URLs before they land in
-- LogAttributes. The CSP spec asks browsers to truncate path+query on
-- cross-origin blocked/document URIs, but enforcement is patchy
-- (Firefox truncates path, Chromium has carve-outs, the Reporting
-- API surfaces the full URL). Without this, any first-party site
-- behind our report-uri leaks its own users' session tokens, magic-
-- link tokens, OAuth `code` params, etc. through the CSP-report
-- channel to anyone with HyperDX access. Scheme+host+path is enough
-- to debug a CSP violation; the query string almost never is.
local function strip_url_meta(url)
    if url == nil or url == "" then return url end
    local q = url:find("[?#]")
    if q then return url:sub(1, q - 1) end
    return url
end

-- host.<hostname> comes from the fluent-bit tag; the upstream input
-- block sets Tag csp.{{ inventory_hostname }}, so the second segment
-- of the tag is what should land in LogAttributes['host'].
local function host_from_tag(tag)
    local dot = string.find(tag, ".", 1, true)
    if dot then return string.sub(tag, dot + 1) end
    return ""
end

function flatten_csp(tag, ts, record)
    -- Every CSP report is a policy violation worth surfacing at warn.
    -- These top-level body keys are mapped to LogRecord.severity_* by
    -- fluent-bit's [OUTPUT] opentelemetry logs_severity_*_message_key
    -- options. 13 is OTLP SEVERITY_NUMBER_WARN.
    record["severity_text"] = "warn"
    record["severity_number"] = 13

    local r, format
    if type(record["csp-report"]) == "table" then
        r = record["csp-report"]
        format = "legacy"
    elseif record["type"] == "csp-violation" and type(record["body"]) == "table" then
        r = record["body"]
        format = "reporting-api"
    else
        -- Unrecognised shape; emit a tagged body so the record is still
        -- discoverable under ServiceName=csplogger with severity=warn,
        -- but with no csp_* attributes.
        record["log"] = 'csplogger received malformed POST '
            .. '{"csp_format":"unknown",'
            .. '"host":"' .. jsonesc(host_from_tag(tag)) .. '"}'
        return 1, ts, record
    end

    local blocked   = strip_url_meta(pick(r, "blocked-uri", "blockedURL"))
    local document  = strip_url_meta(pick(r, "document-uri", "documentURL"))
    local violated  = pick(r, "violated-directive", "violatedDirective",
                              "effective-directive", "effectiveDirective")
    local effective = pick(r, "effective-directive", "effectiveDirective",
                              "violated-directive", "violatedDirective")
    local policy    = pick(r, "original-policy", "originalPolicy")
    local disposition = pick(r, "disposition")
    local referrer    = strip_url_meta(pick(r, "referrer"))
    local source_file = strip_url_meta(pick(r, "source-file", "sourceFile"))
    local sample      = pick(r, "script-sample", "sample")
    local line_no     = pick(r, "line-number", "lineNumber")
    local col_no      = pick(r, "column-number", "columnNumber")
    local status_code = pick(r, "status-code", "statusCode")

    -- Build the JSON suffix. Only emit keys that have values so the
    -- LogAttributes set stays sparse and queries don't trip over
    -- empty strings.
    local parts = {
        '"csp_format":"' .. jsonesc(format) .. '"',
        '"host":"' .. jsonesc(host_from_tag(tag)) .. '"',
    }
    local function add(key, val)
        if val ~= nil and val ~= "" then
            parts[#parts + 1] = '"' .. key .. '":"' .. jsonesc(val) .. '"'
        end
    end
    add("csp_blocked_uri", blocked)
    add("csp_document_uri", document)
    add("csp_violated_directive", violated)
    add("csp_effective_directive", effective)
    add("csp_original_policy", policy)
    add("csp_disposition", disposition)
    add("csp_referrer", referrer)
    add("csp_source_file", source_file)
    add("csp_sample", sample)
    if line_no   ~= nil then parts[#parts + 1] = '"csp_line_number":"'   .. tostring(line_no)   .. '"' end
    if col_no    ~= nil then parts[#parts + 1] = '"csp_column_number":"' .. tostring(col_no)    .. '"' end
    if status_code ~= nil then parts[#parts + 1] = '"csp_status_code":"' .. tostring(status_code) .. '"' end

    record["log"] = string.format(
        "CSP %s blocked %s on %s {%s}",
        effective or "?",
        blocked or "?",
        document or "?",
        table.concat(parts, ",")
    )
    return 2, ts, record
end
