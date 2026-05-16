-- Populate the OTel Resource attributes on every record so HyperDX's
-- ClickHouse exporter writes ServiceName + host.name to otel_logs (the
-- ServiceName column drives the "Services" view; sourced from Resource,
-- not Log attributes). fluent-bit's opentelemetry output reads Resource
-- from metadata.otlp.resource via the logs_metadata_key=otlp namespace.
--
-- LogAttributes and SeverityText/Number are handled separately, in
-- flatten-csp.lua and level-from-message.lua: they write to the same
-- metadata.otlp.{attributes,severity_text,severity_number} stream.
--
-- Match *, runs last in the filter chain so it sees the final tag and
-- the upstream "host" field that the global Set-host modify filter set.
--
-- service.name derivation, keyed on the fluent-bit tag:
--   csp.<host>     -> "csplogger"    (matches what flatten-csp emits)
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

function shape_otlp(tag, ts, group, metadata, record)
    metadata.otlp = metadata.otlp or {}
    local resource_attrs = { ["service.name"] = service_from_tag(tag) }
    local host = record["host"]
    if type(host) == "string" and host ~= "" then
        resource_attrs["host.name"] = host
    end
    metadata.otlp.resource = { attributes = resource_attrs }
    return 1, ts, metadata, record
end
