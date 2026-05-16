-- Flatten browser CSP violation reports (legacy report-uri + modern
-- Reporting API) into one LogRecord per violation. csp_* attributes
-- land directly in LogAttributes via the OTLP metadata stream
-- (metadata.otlp.attributes); severity is pinned to warn/13 via
-- metadata.otlp.severity_{text,number}.
--
-- Wire path: fluent-bit OTLP HTTP output -> hyperdx otelcontribcol's
-- otlp/hyperdx receiver -> ClickHouse otel_logs. fluent-bit's lua filter
-- 5-arg callback writes the event-metadata stream that the opentelemetry
-- output consumes via logs_metadata_key=otlp; LogRecord.Attributes,
-- SeverityText, and SeverityNumber all come from there.
--
-- Body has shape:
--   CSP <effective-directive> blocked <blocked-uri> on <document-uri>
--
-- Service identity (csplogger) is carried in ResourceAttributes by
-- otlp_shape.lua; no need to duplicate it as a LogAttribute.
--
-- Legacy report-uri body  : {"csp-report": {"blocked-uri": "...", ...}}
-- Reporting API body      : [{"type":"csp-violation","body":{"blockedURL":"..."}, ...}]
-- fluent-bit's http input splits the array, so this filter only sees
-- the per-violation shape regardless of which protocol the browser used.
--
-- Trust model: the HTTP input is public-by-design (browsers can't carry
-- auth on a fire-and-forget CSP POST), so `record` arrives entirely
-- attacker-controlled. We discard it at return time and substitute a
-- clean { log = ... } dict — only the synthesised body survives into
-- the downstream record. Without this strip, an attacker could land
-- arbitrary top-level keys (forged `log` body, severity overrides,
-- LogAttribute bloat) that survive through to ClickHouse otel_logs.

local function pick(t, ...)
    if type(t) ~= "table" then return nil end
    for _, k in ipairs({ ... }) do
        local v = t[k]
        local vtype = type(v)
        -- Scalar-only: a malformed POST with a nested object at a known
        -- key (e.g. `{"blocked-uri": {"x":1}}`) would otherwise pass the
        -- table through to scrub(tostring(table)), leaking a Lua heap
        -- pointer ("table: 0x7f...") into LogAttributes.
        if (vtype == "string" or vtype == "number") and v ~= "" then
            return v
        end
    end
    return nil
end

-- Per-field length cap. CSP report fields are tiny in real traffic
-- (URIs, directive names, short policy strings); the cap exists to
-- bound attacker-controlled fields (script-sample, blocked-uri) that
-- could otherwise bloat the record up to nginx's client_max_body_size.
local MAX_FIELD_BYTES = 1024

-- Strip just the unicode bidi-override codepoints (U+202A-U+202E and
-- U+2066-U+2069 in UTF-8) so an attacker-supplied URL can't reorder a
-- log line visually in any UI that renders text as Unicode. Other
-- non-ASCII bytes pass through -- legitimate IDN hostnames and unicode
-- path segments stay readable in HyperDX. Then cap at MAX_FIELD_BYTES.
local function scrub(s)
    if s == nil then return nil end
    s = tostring(s)
    s = s:gsub("\xE2\x80[\xAA-\xAE]", "")
    s = s:gsub("\xE2\x81[\xA6-\xA9]", "")
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

function flatten_csp(tag, ts, group, metadata, record)
    -- Every CSP report is a policy violation worth surfacing at warn.
    -- 13 is OTLP SEVERITY_NUMBER_WARN.
    metadata.otlp = metadata.otlp or {}
    metadata.otlp.severity_text = "warn"
    metadata.otlp.severity_number = 13

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
        -- but with no csp_* attributes beyond csp_format.
        metadata.otlp.attributes = { csp_format = "unknown" }
        return 1, ts, metadata, { log = "csplogger received malformed POST" }
    end

    local blocked     = scrub(strip_url_meta(pick(r, "blocked-uri", "blockedURL")))
    local document    = scrub(strip_url_meta(pick(r, "document-uri", "documentURL")))
    local violated    = scrub(pick(r, "violated-directive", "violatedDirective",
                                       "effective-directive", "effectiveDirective"))
    local effective   = scrub(pick(r, "effective-directive", "effectiveDirective",
                                       "violated-directive", "violatedDirective"))
    local policy      = scrub(pick(r, "original-policy", "originalPolicy"))
    local disposition = scrub(pick(r, "disposition"))
    local referrer    = scrub(strip_url_meta(pick(r, "referrer")))
    local source_file = scrub(strip_url_meta(pick(r, "source-file", "sourceFile")))
    local sample      = scrub(pick(r, "script-sample", "sample"))
    local line_no     = pick(r, "line-number", "lineNumber")
    local col_no      = pick(r, "column-number", "columnNumber")
    local status_code = pick(r, "status-code", "statusCode")

    -- Build LogAttributes as a structured key-value map. Goes through
    -- fluent-bit's OTLP output as metadata.otlp.attributes, landing as
    -- LogRecord.Attributes in ClickHouse otel_logs.
    local attrs = { csp_format = format }
    local function add(key, val)
        if val ~= nil and val ~= "" then attrs[key] = tostring(val) end
    end
    add("csp_blocked_uri",         blocked)
    add("csp_document_uri",        document)
    add("csp_violated_directive",  violated)
    add("csp_effective_directive", effective)
    add("csp_original_policy",     policy)
    add("csp_disposition",         disposition)
    add("csp_referrer",            referrer)
    add("csp_source_file",         source_file)
    add("csp_sample",              sample)
    add("csp_line_number",         line_no)
    add("csp_column_number",       col_no)
    add("csp_status_code",         status_code)
    metadata.otlp.attributes = attrs

    return 1, ts, metadata, {
        log = string.format(
            "CSP %s blocked %s on %s",
            effective or "?",
            blocked or "?",
            document or "?"
        ),
    }
end
