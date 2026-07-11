-- Unit tests for Mosquitto's plain-text Fluent Bit normalizer.

local here = arg[0]:match("^(.*/)") or "./"
dofile(here .. "normalize.lua")

local failures = 0

local function check(label, got, want)
    if got ~= want then
        failures = failures + 1
        print(string.format("FAIL  %s\n        got:  %s\n        want: %s", label, tostring(got), tostring(want)))
    end
end

local function normalize(message, extra, tag)
    local record = { log = message, CONTAINER_TAG = "mosquitto" }
    for key, value in pairs(extra or {}) do
        record[key] = value
    end
    local code = normalize_mosquitto(tag or "svc.mosquitto.service", 0, record)
    return record, code
end

for label, fixture in pairs({
    starting = { "1782267680: mosquitto version 2.0.21 starting\n", "mosquitto version 2.0.21 starting" },
    running = { "1782267680: mosquitto version 2.0.21 running\n", "mosquitto version 2.0.21 running" },
    stopping = { "1782267678: mosquitto version 2.0.21 terminating\n", "mosquitto version 2.0.21 terminating" },
}) do
    local rec = normalize(fixture[1])
    check(label .. ".message", rec.log, fixture[2])
    check(label .. ".event", rec.event, nil)
    check(label .. ".version", rec.version, nil)
end

do
    local rec = normalize("Config loaded from /mosquitto/config/mosquitto.conf.\n")
    check("config.message", rec.log, "Config loaded from /mosquitto/config/mosquitto.conf.")
    check("config.path", rec.config_path, nil)
end

for _, port in ipairs({ 1880, 1883 }) do
    local rec = normalize("Opening ipv4 listen socket on port " .. port .. ".\n")
    check("listener." .. port .. ".message", rec.log, "Opening ipv4 listen socket on port " .. port .. ".")
    check("listener." .. port .. ".port", rec.port, nil)
end

do
    local rec = normalize("1782235460: Saving in-memory database to /mosquitto/data//mosquitto.db.\n")
    check("database.message", rec.log, "Saving in-memory database to /mosquitto/data//mosquitto.db.")
    check("database.event", rec.event, nil)
    check("database.path", rec.database_path, nil)
end

for prefix, level in pairs({ Error = "error", Notice = "notice", Warning = "warn" }) do
    local rec = normalize(prefix .. ": Authentication failed for client sensor")
    check("level." .. prefix .. ".message", rec.log, "Authentication failed for client sensor")
    check("level." .. prefix .. ".level", rec._level, level)
end

do
    local rec = normalize("Future broker message")
    check("unknown.message", rec.log, "Future broker message")
    check("unknown.status", rec.parser_status, nil)
    check("unknown.error", rec.parse_error, nil)
end

do
    local rec = normalize("mosquitto version 2.0.21 running", {
        CONTAINER_ID = "123456789abc",
        CONTAINER_ID_FULL = "123456789abcdef0",
        CMDLINE = "/usr/bin/conmon --log-tag mosquitto",
        PRIORITY = "6",
        SYSTEMD_UNIT = "mosquitto.service",
        SYSLOG_IDENTIFIER = "mosquitto",
    })
    check("envelope.short_container_id_kept", rec.CONTAINER_ID, "123456789abc")
    check("envelope.container_tag_removed", rec.CONTAINER_TAG, nil)
    check("envelope.full_container_id_removed", rec.CONTAINER_ID_FULL, nil)
    check("envelope.cmdline_removed", rec.CMDLINE, nil)
    check("envelope.priority_removed", rec.PRIORITY, nil)
    check("envelope.unit_removed", rec.SYSTEMD_UNIT, nil)
    check("envelope.identifier_removed", rec.SYSLOG_IDENTIFIER, nil)
end

do
    local record = { log = "podman command output" }
    local code = normalize_mosquitto("svc.mosquitto.service", 0, record)
    check("unit_output.code", code, 0)
    check("unit_output.status", record.parser_status, nil)
    check("unit_output.raw", record.log, "podman command output")
end

do
    local rec, code = normalize("untouched", nil, "svc.other.service")
    check("tag.code", code, 0)
    check("tag.container_tag", rec.CONTAINER_TAG, "mosquitto")
end

if failures > 0 then
    os.exit(1)
end
print("PASS  mosquitto normalize")
