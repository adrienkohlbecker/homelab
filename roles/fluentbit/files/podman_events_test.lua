-- Unit tests for podman_events.lua. Run via `mise run test:fluentbit-lua`.

local here = arg[0]:match("^(.*/)") or "./"
dofile(here .. "podman_events.lua")

local function check(label, got, want)
    assert(got == want, string.format("%s: got %s, want %s", label, tostring(got), tostring(want)))
end

local function normalize(record)
    local code, _, normalized = normalize_podman_event("svc.example.service", 0, record)
    return normalized, code
end

do
    local cid = "ae47b4d09bf4d0167c83ef5be1f432d22e21ec72b3472d2bb46b2a55c91d5a9e"
    local rec, code = normalize({
        MESSAGE = "2026-07-13 02:01:25.445274426 +0000 UTC m=+1.145839344 container start " .. cid,
        SYSLOG_IDENTIFIER = "podman",
        PODMAN_EVENT = "start",
        PODMAN_TYPE = "container",
        PODMAN_NAME = "homeassistant",
        PODMAN_IMAGE = "ghcr.io/home-assistant/home-assistant:2026.6.3",
        PODMAN_ID = cid,
        PODMAN_HEALTH_STATUS = "",
        PODMAN_TIME = "2026-07-13T02:01:25.445274426Z",
        PODMAN_LABELS = '{"org.opencontainers.image.description":"Open-source home automation platform"}',
    })
    check("start.code", code, 1)
    check("start.message", rec.MESSAGE, "container start homeassistant")
    check("start.event", rec.event, "start")
    check("start.object_type", rec.object_type, "container")
    check("start.name", rec.name, "homeassistant")
    check("start.image", rec.image, "ghcr.io/home-assistant/home-assistant:2026.6.3")
    check("start.object_id", rec.object_id, cid)
    check("start.no_native_event", rec.PODMAN_EVENT, nil)
    check("start.no_empty_health", rec.health_status, nil)
    check("start.no_time", rec.PODMAN_TIME, nil)
    check("start.no_labels", rec.PODMAN_LABELS, nil)
end

do
    local _, code = normalize({
        MESSAGE = "verbose health event",
        SYSLOG_IDENTIFIER = "podman",
        PODMAN_EVENT = "health_status",
        PODMAN_TYPE = "container",
        PODMAN_NAME = "nexus",
        PODMAN_ID = "1698fd043abc036968649389789b79b5b68f772cce6c69ed09053dae035a7021",
        PODMAN_HEALTH_STATUS = "starting",
    })
    check("health.starting.code", code, -1)
end

do
    local _, code = normalize({
        MESSAGE = "verbose health event",
        SYSLOG_IDENTIFIER = "podman",
        PODMAN_EVENT = "health_status",
        PODMAN_TYPE = "container",
        PODMAN_NAME = "nexus",
        PODMAN_HEALTH_STATUS = "healthy",
    })
    check("health.healthy.code", code, -1)
end

do
    local rec, code = normalize({
        MESSAGE = "verbose health event",
        SYSLOG_IDENTIFIER = "podman",
        PODMAN_EVENT = "health_status",
        PODMAN_TYPE = "container",
        PODMAN_NAME = "nexus",
        PODMAN_HEALTH_STATUS = "unhealthy",
    })
    check("health.unhealthy.code", code, 1)
    check("health.unhealthy.message", rec.MESSAGE, "container health_status nexus (health=unhealthy)")
    check("health.unhealthy.status", rec.health_status, "unhealthy")
end

do
    local rec = normalize({
        MESSAGE = "verbose died event",
        SYSLOG_IDENTIFIER = "podman",
        PODMAN_EVENT = "died",
        PODMAN_TYPE = "container",
        PODMAN_NAME = "worker",
        PODMAN_EXIT_CODE = "125",
    })
    check("died.message", rec.MESSAGE, "container died worker (exit=125)")
    check("died.exit_code", rec.exit_code, 125)
end

do
    local rec = normalize({
        MESSAGE = "verbose image pull event",
        SYSLOG_IDENTIFIER = "podman",
        PODMAN_EVENT = "pull",
        PODMAN_TYPE = "image",
        PODMAN_NAME = "docker.io/library/busybox:latest",
        PODMAN_ID = "4bd29e98c23c15060696b0503b60864023ecea76c0bc71a4e6ae2c3c2b71348e",
    })
    check("pull.message", rec.MESSAGE, "image pull docker.io/library/busybox:latest")
end

do
    local rec = normalize({
        MESSAGE = "2026-06-26 07:14:10 system refresh",
        SYSLOG_IDENTIFIER = "podman",
        PODMAN_EVENT = "refresh",
        PODMAN_TYPE = "system",
    })
    check("refresh.message", rec.MESSAGE, "system refresh")
end

do
    local message = 'time="2026-07-10T02:07:30Z" level=info msg="Using sqlite as database backend"'
    local rec, code = normalize({ MESSAGE = message, SYSLOG_IDENTIFIER = "podman" })
    check("daemon.code", code, 0)
    check("daemon.message", rec.MESSAGE, message)
end

do
    local rec, code = normalize({
        MESSAGE = "application record",
        SYSLOG_IDENTIFIER = "conmon",
        PODMAN_EVENT = "start",
        PODMAN_TYPE = "container",
    })
    check("non_podman.code", code, 0)
    check("non_podman.message", rec.MESSAGE, "application record")
    check("non_podman.event_preserved", rec.PODMAN_EVENT, "start")
end
