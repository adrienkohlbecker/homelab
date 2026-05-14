-- Populate the OTel Resource attributes on every record so HyperDX's
-- ClickHouse exporter writes ServiceName + host.name to otel_logs (the
-- ServiceName column drives the "Services" view; sourced from Resource,
-- not Log attributes). fluent-bit's opentelemetry output reads the
-- Resource from record[logs_resource_metadata_key], which the [OUTPUT]
-- block pins to "resource" (a body key the lua filter writes).
--
-- LogAttributes population and SeverityText/Number are handled
-- separately, in roles/fluentbit/files/flatten-csp.lua and similar
-- per-source filters: those keys can't be shipped through fluent-bit's
-- OTLP output to the OTel collector directly (the logs_*_metadata_key
-- options read from fluent-bit's event-metadata stream, which lua
-- filters have no API to populate). Instead each filter emits a Body
-- containing a level keyword (warn/error/...) and a JSON object
-- suffix, which the HyperDX collector's transform processor then
-- expands into log.attributes and infers severity from. This filter
-- stays minimal and Resource-only.
--
-- Match *, runs last in the filter chain so it sees the final tag and
-- the upstream "host" field that the global Add-host modify filter set.
--
-- service.name derivation, keyed on the fluent-bit tag:
--   csp.<host>     -> "csplogger"    (matches the SYSLOG_IDENTIFIER flatten-csp emits)
--   svc.<name>     -> "<name>"       (rewrite_tag derives from journald)
--   nginx.access   -> "nginx_access" (tail input tag, no rewrite)
--   nginx.error    -> "nginx_error"
--   journal.<...>  -> "journal"      (rewrite_tag rule didn't match)
--   <other>        -> tag verbatim   (fallback)

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
    local resource_attrs = { ["service.name"] = service_from_tag(tag) }
    local host = record["host"]
    if type(host) == "string" and host ~= "" then
        resource_attrs["host.name"] = host
    end
    record["resource"] = { attributes = resource_attrs }
    return 2, ts, record
end
