-- Restructure flat fluent-bit records into the OTLP-native shape that
-- fluent-bit's opentelemetry output serialises end-to-end. The output
-- reads per-record OTel fields from configurable metadata keys:
--   logs_body_key             (default "log")      -> LogRecord.Body
--   logs_metadata_key         (default "otlp")     -> LogRecord.{Attributes,Severity,Trace,Span}
--   logs_resource_metadata_key(default "resource") -> LogRecord.Resource
-- Top-level record keys outside this recognised set are silently dropped
-- on serialization. Without this filter, host / SYSLOG_IDENTIFIER /
-- csp_* etc. reach fluent-bit but never reach ClickHouse, and the
-- ServiceName column (which the ClickHouse exporter populates from
-- Resource attributes, not Log attributes) stays blank for every record.
--
-- Match *, runs last in the filter chain so it sees the final flat
-- record shape produced by the earlier filters (Add host, Copy
-- MESSAGE log, flatten-csp.lua, level-from-message.lua).
--
-- service.name derivation, keyed on the fluent-bit tag:
--   csp.<host>     -> "csplogger"    (the SYSLOG_IDENTIFIER flatten-csp sets)
--   svc.<name>     -> "<name>"       (rewrite_tag derives from journald)
--   nginx.access   -> "nginx_access" (tail input tag, no rewrite)
--   nginx.error    -> "nginx_error"
--   journal.<...>  -> "journal"      (rewrite_tag rule didn't match)
--   <other>        -> tag verbatim   (fallback)

local skip = {
    -- Mapped to OTLP-native fields by fluent-bit itself; promoting them
    -- to LogAttributes too would duplicate or shadow.
    log = true,
    otlp = true,
    resource = true,
    timestamp = true,
    observed_timestamp = true,
    severity_text = true,
    severity_number = true,
    trace_id = true,
    span_id = true,
    trace_flags = true,
    -- The journal's source key for Body: the upstream Copy MESSAGE log
    -- filter already mirrored it to "log", so keeping MESSAGE in
    -- LogAttributes would store the body twice.
    MESSAGE = true,
}

local function service_from_tag(tag)
    if string.sub(tag, 1, 4) == "csp." then
        return "csplogger"
    end
    if string.sub(tag, 1, 4) == "svc." then
        return string.sub(tag, 5)
    end
    if string.sub(tag, 1, 6) == "nginx." then
        return "nginx_" .. string.sub(tag, 7)
    end
    if string.sub(tag, 1, 8) == "journal." then
        return "journal"
    end
    return tag
end

function shape_otlp(tag, ts, record)
    local attrs = {}
    for k, v in pairs(record) do
        if not skip[k] then
            attrs[k] = v
        end
    end

    local otlp = { attributes = attrs }
    -- Promote severity fields if an upstream filter (e.g. flatten-csp.lua
    -- for CSP records) set them at the top level. fluent-bit's OTLP
    -- output reads severity_text / severity_number from inside the
    -- metadata sub-map, not from the record root. Leaving them unset
    -- here means HyperDX's transform processor will fall back to
    -- body-keyword inference for that record.
    if record["severity_text"] ~= nil then
        otlp.severity_text = record["severity_text"]
    end
    if record["severity_number"] ~= nil then
        otlp.severity_number = record["severity_number"]
    end
    record["otlp"] = otlp

    local resource_attrs = { ["service.name"] = service_from_tag(tag) }
    local host = record["host"]
    if type(host) == "string" and host ~= "" then
        resource_attrs["host.name"] = host
    end
    record["resource"] = { attributes = resource_attrs }

    return 2, ts, record
end
