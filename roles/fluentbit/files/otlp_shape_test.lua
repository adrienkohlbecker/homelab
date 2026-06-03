-- Unit tests for otlp_shape.lua. Run via `mise run test:fluentbit-lua`
-- (which locates a system lua); or directly: `lua5.4 otlp_shape_test.lua`.
-- Exits non-zero on the first failed assertion.
--
-- The records mirror real journald fields seen on lab (SYSLOG_IDENTIFIER,
-- _SYSTEMD_UNIT, UNIT) -- the systemd input tags every journal record
-- `svc.<_SYSTEMD_UNIT>`, so the tags here match what fluent-bit emits. The
-- 64-hex UNIT in the healthcheck case is a real container id captured from
-- `journalctl _PID=1`. They exercise every service_from_tag branch plus the
-- podman-healthcheck override and the attribute-lifting loop.

local here = arg[0]:match("^(.*/)") or "./"
dofile(here .. "otlp_shape.lua")

local failures = 0

local function check(label, got, want)
    if got ~= want then
        failures = failures + 1
        print(string.format("FAIL  %s\n        got:  %s\n        want: %s", label, tostring(got), tostring(want)))
    end
end

-- Run a (tag, record) through the filter; return the service.name it derived,
-- the resource attrs table, and the lifted LogAttributes table.
local function shape(tag, record)
    record = record or {}
    local metadata = {}
    shape_otlp(tag, 0, nil, metadata, record)
    local ra = (record.resource or {}).attributes or {}
    return ra["service.name"], ra, (metadata.otlp or {}).attributes or {}, record
end

-- 1. svc.* with SYSLOG_IDENTIFIER -> the identifier, lowercased
--    (Keepalived_vrrp -> keepalived_vrrp). host.name lifted from `host`.
do
    local svc, ra = shape("svc.keepalived.service", { SYSLOG_IDENTIFIER = "Keepalived_vrrp", host = "lab" })
    check("svc.identifier", svc, "keepalived_vrrp")
    check("svc.host_name", ra["host.name"], "lab")
end

-- 2. Kernel records carry SYSLOG_IDENTIFIER=kernel but no _SYSTEMD_UNIT (tag
--    suffix empty) -- the identifier still gives a real facet.
do
    local svc = shape("svc.", { SYSLOG_IDENTIFIER = "kernel" })
    check("svc.kernel", svc, "kernel")
end

-- 3. Mitogen worker identifiers collapse onto a single "mitogen" facet.
do
    local svc = shape("svc.session-3.scope", { SYSLOG_IDENTIFIER = "python3(mitogen:ak@lab:12345)" })
    check("svc.mitogen", svc, "mitogen")
end

-- 4. Unnamed podman container (libpod-conmon scope) -> podman_unnamed,
--    regardless of the auto-generated SYSLOG_IDENTIFIER nickname.
do
    local svc = shape("svc.libpod-conmon-abc123.scope", { SYSLOG_IDENTIFIER = "epic_allen" })
    check("svc.podman_unnamed", svc, "podman_unnamed")
end

-- 5. svc.* with no SYSLOG_IDENTIFIER -> fall back to the unit name from the
--    tag, .service stripped.
do
    local svc = shape("svc.cron.service", {})
    check("svc.unit_fallback", svc, "cron")
end

-- 6. CSP ingest tag -> csplogger (matches what flatten_csp emits).
do
    local svc = shape("csp.lab", { log = "CSP script-src blocked x on y" })
    check("csp.service", svc, "csplogger")
end

-- 7/8. nginx tail tags -> nginx_access / nginx_error.
do
    check("nginx.access", (shape("nginx.access", {})), "nginx_access")
    check("nginx.error", (shape("nginx.error", {})), "nginx_error")
end

-- 9. Bare "nginx." with empty subtag -> unknown (not the nameless "nginx_").
do
    local svc = shape("nginx.", {})
    check("nginx.bare", svc, "unknown")
end

-- 10. Any other tag is taken verbatim, lowercased (tail inputs like
--     host_packages, audit, pihole_ftl).
do
    check("other.verbatim", (shape("host_packages", {})), "host_packages")
    check("other.lowercased", (shape("Audit", {})), "audit")
end

-- 11. Podman healthcheck override: PID 1 emits a lifecycle line whose journal
--     UNIT attribution is a 64-hex <container-id>.service. service.name is
--     forced to podman_healthcheck and CONTAINER_ID{,_FULL} are stamped (and
--     thus lifted into LogAttributes) for the HyperDX pivot. Real container id.
do
    local cid = "75ca2e2b110c2a3e6af421033e99bc1dbc8f58d3eacf929cb2b395377d63e4bc"
    local svc, _, attrs, rec = shape("svc.init.scope", { SYSLOG_IDENTIFIER = "systemd", UNIT = cid .. ".service" })
    check("healthcheck.service", svc, "podman_healthcheck")
    check("healthcheck.cid_full", rec.CONTAINER_ID_FULL, cid)
    check("healthcheck.cid_short", rec.CONTAINER_ID, string.sub(cid, 1, 12))
    check("healthcheck.cid_full_lifted", attrs.CONTAINER_ID_FULL, cid)
    check("healthcheck.cid_short_lifted", attrs.CONTAINER_ID, string.sub(cid, 1, 12))
end

-- 12. A non-hex / wrong-length UNIT must NOT trigger the healthcheck override.
do
    local svc = shape("svc.cron.service", { UNIT = "cron.service" })
    check("healthcheck.no_override", svc, "cron")
end

-- 13. Attribute lifting + exclusions: every record key except the four routed
--     elsewhere (log, resource, severity_text, severity_number) lands in
--     LogAttributes. service.name lives in resource attrs, not LogAttributes.
do
    local record = {
        log = "the body",
        severity_text = "info",
        severity_number = 9,
        PID = 1234,
        SYSLOG_IDENTIFIER = "sshd",
        host = "lab",
    }
    local svc, ra, attrs = shape("svc.ssh.service", record)
    check("lift.service", svc, "sshd")
    check("lift.pid", attrs.PID, 1234)
    check("lift.ident", attrs.SYSLOG_IDENTIFIER, "sshd")
    check("lift.host", attrs.host, "lab")
    check("lift.no_log", attrs.log, nil)
    check("lift.no_resource", attrs.resource, nil)
    check("lift.no_sevtext", attrs.severity_text, nil)
    check("lift.no_sevnum", attrs.severity_number, nil)
    check("lift.service_not_in_attrs", attrs["service.name"], nil)
    check("lift.host_name_in_resource", ra["host.name"], "lab")
end

-- 14. host.name is omitted from resource attrs when there's no `host` field
--     (empty string also counts as absent).
do
    local _, ra = shape("svc.x.service", { SYSLOG_IDENTIFIER = "x", host = "" })
    check("nohost.absent", ra["host.name"], nil)
end

if failures == 0 then
    print("otlp_shape: all assertions passed")
    os.exit(0)
else
    print(string.format("otlp_shape: %d assertion(s) failed", failures))
    os.exit(1)
end
