-- Unit tests for lnav_shape.lua. Run via `mise run test:fluentbit-lua`
-- or directly with lua5.4.

local here = arg[0]:match("^(.*/)") or "./"
dofile(here .. "lnav_shape.lua")

local failures = 0

local function check(label, got, want)
    if got ~= want then
        failures = failures + 1
        print(string.format("FAIL  %s\n        got:  %s\n        want: %s", label, tostring(got), tostring(want)))
    end
end

local function shape(tag, record, severity_text, severity_number)
    record = record or {}
    local metadata = { otlp = { severity_text = severity_text, severity_number = severity_number } }
    local _, _, _, shaped = shape_lnav(tag, 0, nil, metadata, record)
    return shaped
end

do
    local rec = shape("svc.homeassistant.service", {
        host = "lab",
        log = "WARNING connection failed",
        SYSTEMD_UNIT = "homeassistant.service",
        SYSLOG_IDENTIFIER = "homeassistant",
        CONTAINER_NAME = "homeassistant",
    }, "warn", 13)
    check("journald.host", rec.host, "lab")
    check("journald.service", rec.service, "homeassistant")
    check("journald.unit", rec.unit, "homeassistant.service")
    check("journald.identifier", rec.identifier, "homeassistant")
    check("journald.level", rec.level, "warn")
    check("journald.level_number", rec.level_number, 13)
    check("journald.message", rec.message, "WARNING connection failed")
    check("journald.stream", rec.stream, "journald")
    check("journald.fields.systemd_unit", rec.fields.SYSTEMD_UNIT, "homeassistant.service")
    check("journald.fields.container_name", rec.fields.CONTAINER_NAME, "homeassistant")
    check("journald.fields.no_log", rec.fields.log, nil)
end

do
    local rec =
        shape("svc.session-3.scope", { SYSLOG_IDENTIFIER = "python3(mitogen:ak@lab:12345)", log = "ok" }, "info", 9)
    check("mitogen.service", rec.service, "mitogen")
end

do
    local rec = shape("svc.libpod-conmon-abc123.scope", { SYSLOG_IDENTIFIER = "epic_allen", log = "ok" }, "info", 9)
    check("podman_unnamed.service", rec.service, "podman_unnamed")
end

do
    local rec = shape(
        "nginx.access",
        { host = "lab", log = "GET / HTTP/1.1", filepath = "/var/log/nginx/access.log" },
        "info",
        9
    )
    check("nginx.service", rec.service, "nginx_access")
    check("nginx.stream", rec.stream, "nginx")
    check("nginx.filepath", rec.fields.filepath, "/var/log/nginx/access.log")
end

do
    local cid = "75ca2e2b110c2a3e6af421033e99bc1dbc8f58d3eacf929cb2b395377d63e4bc"
    local rec =
        shape("svc.init.scope", { SYSLOG_IDENTIFIER = "systemd", UNIT = cid .. ".service", log = cid }, "info", 9)
    check("healthcheck.service", rec.service, "podman_healthcheck")
    check("healthcheck.cid_full", rec.fields.CONTAINER_ID_FULL, cid)
    check("healthcheck.cid_short", rec.fields.CONTAINER_ID, string.sub(cid, 1, 12))
end

do
    local rec = shape("host_packages", { host = "lab", log = "updated\npackage", severity_text = "debug" }, nil, nil)
    check("fallback.level", rec.level, "info")
    check("fallback.level_number", rec.level_number, 9)
    check("scrub.message", rec.message, "updated package")
    check("fields.no_severity_text", rec.fields.severity_text, nil)
end

if failures == 0 then
    print("lnav_shape: all assertions passed")
    os.exit(0)
else
    print(string.format("lnav_shape: %d assertion(s) failed", failures))
    os.exit(1)
end
