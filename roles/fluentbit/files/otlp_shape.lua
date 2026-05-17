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
-- This filter also lifts EVERY remaining record key into
-- metadata.otlp.attributes so they land as LogAttributes -- the OTLP
-- output reads attributes via $otlp['attributes'] only, so anything
-- left in the record body is dropped on the floor. For journald records
-- this is how SYSTEMD_UNIT / SYSLOG_IDENTIFIER / PID / COMM / PRIORITY
-- etc. reach HyperDX. The upstream "host" field (set by the global
-- Set-host modify filter) gets lifted by the same loop. For CSP records,
-- flatten_csp.lua replaces the record wholesale and pre-populates
-- metadata.otlp.attributes with csp_*; the loop here only adds "host".
-- Excluded keys (routed elsewhere or already lifted):
--   log              -> LogRecord.Body
--   resource         -> ResourceLogs.resource (set just below)
--   severity_text    -> metadata.otlp.severity_text (lifted by
--   severity_number     level_from_message.lua / modify pre-stamp)
--
-- Match *, runs last in the filter chain so it sees the final tag and
-- the upstream "host" field that the global Set-host modify filter set.
--
-- service.name derivation:
--   record.UNIT =  -> "podman_healthcheck"  (overrides; see below)
--   <64hex>.service
--   csp.<host>     -> "csplogger"    (matches what flatten_csp emits)
--   svc.*          -> SYSLOG_IDENTIFIER (paren-stripped), else
--                     _SYSTEMD_UNIT (from tag, .service-stripped),
--                     else "unknown". Sourced from the journal record;
--                     the systemd input's `svc.*` tag is just a
--                     routing prefix, the real name picks come from
--                     fields on the record. Detail below.
--   nginx.access   -> "nginx_access" (tail input tag)
--   nginx.error    -> "nginx_error"
--   empty suffix   -> "unknown"      (bare nginx. with no subtag;
--                                     without this the result would be
--                                     "nginx_" and HyperDX would merge
--                                     those records under a nameless
--                                     facet.)
--   <other>        -> tag verbatim
--
-- Why SYSLOG_IDENTIFIER over _SYSTEMD_UNIT for the svc.* branch:
--   * Kernel records (_TRANSPORT=kernel, no _SYSTEMD_UNIT) carry
--     SYSLOG_IDENTIFIER=kernel — nftables LOG drops, oom-killer, MCE,
--     audit etc. land under a real facet instead of "unknown".
--   * Sub-events inside wrapper units carry their own identifier:
--     sudo / sshd / vsce-sign inside session-N.scope, systemd inside
--     init.scope. The wrapper unit name is a routing artefact; the
--     identifier is what the operator typed or what the writing
--     binary called itself.
--   * Podman containers run with `--log-opt tag=<name>` get
--     SYSLOG_IDENTIFIER=<name>, which equals (or refines) the wrapping
--     <name>.service. No regression for the common containerised case.
--
-- Mitogen special case: the ansible accelerator names its worker
-- processes "python3(mitogen:user@host:PID)" via prctl(PR_SET_NAME),
-- so each ansible apply mints a fresh SYSLOG_IDENTIFIER facet per
-- worker PID. Collapse all such records onto a single "mitogen"
-- facet (more meaningful than the stripped "python3", which loses
-- the "this is an ansible run" signal). Substring match on
-- "(mitogen:" — that segment is mitogen's official self-naming
-- convention so it's reasonably stable; any other parenthesised
-- identifier reaches us as-is.
--
-- Unnamed-container fallback: podman runs without `--name` get an
-- auto-generated SYSLOG_IDENTIFIER ("<adjective>_<scientist>", fresh
-- per container) under a "libpod-conmon-<hash>.scope" _SYSTEMD_UNIT.
-- Both fields are unbounded, so we can't recover the call-site name
-- from the record. Detect the conmon-scope prefix on the tag and
-- label as "podman_unnamed" — flags the records as a class so the
-- operator knows to track down the launcher and add `--name <fixed>`.
-- Real fix is at the launcher (in-repo: ansible role / unit
-- template; out-of-repo: operator shell command); this filter just
-- keeps the Services facet bounded until then.
--
-- Podman healthcheck transient units: each scheduled check runs as
-- `systemd-run --unit=<container-id>` where the unit name is the full
-- 64-char container ID + ".service". The lifecycle records ("Started
-- <hash>.service", "Failed with result 'exit-code'", "Deactivated
-- successfully") are emitted by PID 1, so the record's _SYSTEMD_UNIT
-- is init.scope (not the ephemeral unit) and SYSLOG_IDENTIFIER is
-- "systemd" — without this override they'd bucket under
-- service.name=systemd alongside legitimate init noise. The journal-
-- attribution field UNIT=<64hex>.service is what carries the actual
-- target; key on that. Stamp CONTAINER_ID_FULL + CONTAINER_ID on the
-- record so they get lifted into LogAttributes by the loop below,
-- matching exactly what podman's journald log driver puts on the
-- container's own application logs — enables a HyperDX pivot from a
-- failed healthcheck to that container's output without any host-side
-- container_id→name lookup at log time. Heuristic risk: any unrelated
-- `systemd-run --unit=<64hex>` would also bucket here; in practice
-- nothing else uses pure-hex unit names.
--
-- Lowercased on the way out. Unit names and identifiers are
-- mixed-case (Keepalived_vrrp, NetworkManager, CRON, ...), awkward to
-- type in HyperDX's UI and to group on if a service spells itself
-- differently across releases.

local function service_from_tag(tag, record)
    local svc
    if string.sub(tag, 1, 4) == "csp." then
        svc = "csplogger"
    elseif string.sub(tag, 1, 4) == "svc." then
        if string.sub(tag, 1, 18) == "svc.libpod-conmon-" then
            -- Unnamed podman container. SYSLOG_IDENTIFIER is podman's
            -- auto-generated nickname (epic_allen, etc.), unbounded.
            svc = "podman_unnamed"
        else
            local sysid = record["SYSLOG_IDENTIFIER"]
            if type(sysid) == "string" and sysid ~= "" then
                if sysid:find("(mitogen:", 1, true) then
                    svc = "mitogen"
                else
                    svc = sysid
                end
            end
            if svc == nil or svc == "" then
                -- SYSLOG_IDENTIFIER missing on the record (rare;
                -- journald sets it from comm by default). Fall back
                -- to _SYSTEMD_UNIT via the tag.
                svc = string.sub(tag, 5):gsub("%.service$", "")
            end
        end
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

local EXCLUDE_FROM_ATTRS = {
    log = true, resource = true,
    severity_text = true, severity_number = true,
}

function shape_otlp(tag, ts, group, metadata, record)
    metadata.otlp = metadata.otlp or {}
    metadata.otlp.attributes = metadata.otlp.attributes or {}

    -- Podman healthcheck override: when PID 1 emits a lifecycle message
    -- about a transient <64hex>.service unit, route it to a dedicated
    -- service.name and stamp CONTAINER_ID_FULL + CONTAINER_ID (matching
    -- podman's journald-driver field names on the container's app logs)
    -- so HyperDX can pivot. See file header for the full rationale.
    local svc_override
    local unit = record["UNIT"]
    if type(unit) == "string" then
        local cid = unit:match("^([0-9a-f]+)%.service$")
        if cid and #cid == 64 then
            svc_override = "podman_healthcheck"
            record["CONTAINER_ID_FULL"] = cid
            record["CONTAINER_ID"] = string.sub(cid, 1, 12)
        end
    end

    local resource_attrs = { ["service.name"] = svc_override or service_from_tag(tag, record) }
    local host = record["host"]
    if type(host) == "string" and host ~= "" then
        resource_attrs["host.name"] = host
    end
    record["resource"] = { attributes = resource_attrs }

    for k, v in pairs(record) do
        if not EXCLUDE_FROM_ATTRS[k] then
            metadata.otlp.attributes[k] = v
        end
    end

    return 1, ts, metadata, record
end
