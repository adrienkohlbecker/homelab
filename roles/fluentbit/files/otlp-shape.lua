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
--
-- Lowercased on the way out. Journal SYSLOG_IDENTIFIERs are mixed-case
-- (Keepalived_vrrp, NetworkManager, etc.) which is awkward both to type
-- in HyperDX's UI filter and to group on when one service identifies
-- itself inconsistently across releases. Normalising to lowercase here
-- keeps the Services facet tidy and case-insensitive at the source.

local function service_from_tag(tag)
    local svc
    if string.sub(tag, 1, 4) == "csp." then
        svc = "csplogger"
    elseif string.sub(tag, 1, 4) == "svc." then
        svc = string.sub(tag, 5)
    elseif string.sub(tag, 1, 6) == "nginx." then
        svc = "nginx_" .. string.sub(tag, 7)
    elseif string.sub(tag, 1, 8) == "journal." then
        svc = "journal"
    else
        svc = tag
    end
    return string.lower(svc)
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
