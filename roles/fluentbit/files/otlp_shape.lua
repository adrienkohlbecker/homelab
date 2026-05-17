-- Populate the OTel Resource attributes on every record so HyperDX's
-- ClickHouse exporter writes ServiceName + host.name to otel_logs (the
-- ServiceName column drives the "Services" view; sourced from Resource,
-- not Log attributes). fluent-bit's opentelemetry output reads Resource
-- via the hardcoded $resource['attributes'] record accessor applied to
-- each regular log record's body in "standalone context" mode (see
-- out_opentelemetry's logs.c standalone branch -> goto start_resource).
-- The config key logs_resource_metadata_key=resource pins it; we write
-- to record["resource"] (BODY, not the event-metadata stream). The
-- metadata stream has no $otlp['resource'] accessor -- only severity_*,
-- attributes, timestamps, trace/span IDs -- so a metadata.otlp.resource
-- write is a dead letter (commit f59a9e99 made that mistake; this is
-- the revert of just the Resource path).
--
-- LogAttributes and SeverityText/Number ARE plumbed through the
-- metadata stream, in flatten_csp.lua and level_from_message.lua:
-- $otlp['attributes'] and $otlp['severity_*'] are wired record
-- accessors in the OTLP output, unlike resource.
--
-- This filter also lifts the upstream "host" field (set by the global
-- Set-host modify filter) into metadata.otlp.attributes so it lands
-- as a LogAttribute next to the CSP / journald / nginx record stream.
-- For CSP records, flatten_csp.lua replaces the record wholesale, so
-- "host" would otherwise never reach LogAttributes; this is the only
-- place it can be reattached without re-deriving from the tag.
--
-- Match *, runs last in the filter chain so it sees the final tag and
-- the upstream "host" field that the global Set-host modify filter set.
--
-- service.name derivation, keyed on the fluent-bit tag:
--   csp.<host>     -> "csplogger"    (matches what flatten_csp emits)
--   svc.<unit>     -> "<unit>"       (systemd input's Tag svc.* wildcard
--                                     expands _SYSTEMD_UNIT, e.g.
--                                     nginx.service -> "nginx"; only
--                                     .service is stripped — .timer,
--                                     .socket, .scope etc. stay so the
--                                     Services facet doesn't merge a
--                                     daemon's runtime logs with its
--                                     timer-trigger events)
--   nginx.access   -> "nginx_access" (tail input tag)
--   nginx.error    -> "nginx_error"
--   empty suffix   -> "unknown"      (svc. with no _SYSTEMD_UNIT, bare
--                                     nginx. with no subtag — without
--                                     this the result would be "" /
--                                     "nginx_" and HyperDX would group
--                                     every such record under a single
--                                     nameless facet)
--   <other>        -> tag verbatim
--
-- Lowercased on the way out. Unit names are mixed-case (Keepalived_vrrp,
-- NetworkManager, etc.) which is awkward both to type in HyperDX's UI
-- filter and to group on when one service identifies itself
-- inconsistently across releases. Normalising to lowercase here keeps
-- the Services facet tidy and case-insensitive at the source.

local function service_from_tag(tag)
    local svc
    if string.sub(tag, 1, 4) == "csp." then
        svc = "csplogger"
    elseif string.sub(tag, 1, 4) == "svc." then
        svc = string.sub(tag, 5):gsub("%.service$", "")
    elseif string.sub(tag, 1, 6) == "nginx." then
        -- Only matches the tail-input tags above (nginx.access /
        -- nginx.error). nginx.service journal records arrive as
        -- svc.nginx.service and hit the svc. branch (yielding "nginx").
        local sub = string.sub(tag, 7)
        if sub ~= "" then svc = "nginx_" .. sub end
    else
        svc = tag
    end
    if svc == nil or svc == "" then svc = "unknown" end
    return string.lower(svc)
end

function shape_otlp(tag, ts, group, metadata, record)
    metadata.otlp = metadata.otlp or {}
    metadata.otlp.attributes = metadata.otlp.attributes or {}

    local resource_attrs = { ["service.name"] = service_from_tag(tag) }
    local host = record["host"]
    if type(host) == "string" and host ~= "" then
        resource_attrs["host.name"] = host
        metadata.otlp.attributes["host"] = host
    end
    record["resource"] = { attributes = resource_attrs }
    return 1, ts, metadata, record
end
