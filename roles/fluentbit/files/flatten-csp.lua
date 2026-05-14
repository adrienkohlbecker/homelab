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

-- Minimal JSON string escaper: backslash and double-quote (everything
-- else in CSP-report bodies is URL-safe ASCII in practice). The
-- downstream consumer is otelcontribcol's ParseJSON, which is strict --
-- a malformed value just means that record's attributes don't get
-- extracted, not a pipeline failure. Belt-and-braces: also strip
-- control chars below 0x20 since browsers occasionally include
-- tab/newline in script-sample for multi-line inline scripts.
local function jsonesc(s)
    if s == nil then return "" end
    s = tostring(s)
    s = s:gsub("\\", "\\\\"):gsub('"', '\\"')
    s = s:gsub("[%c]", "")
    return s
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

    local blocked   = pick(r, "blocked-uri", "blockedURL")
    local document  = pick(r, "document-uri", "documentURL")
    local violated  = pick(r, "violated-directive", "violatedDirective",
                              "effective-directive", "effectiveDirective")
    local effective = pick(r, "effective-directive", "effectiveDirective",
                              "violated-directive", "violatedDirective")
    local policy    = pick(r, "original-policy", "originalPolicy")
    local disposition = pick(r, "disposition")
    local referrer    = pick(r, "referrer")
    local source_file = pick(r, "source-file", "sourceFile")
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
