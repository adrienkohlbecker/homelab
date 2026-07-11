-- Clean Mosquitto's plain-text container output after journald has supplied the
-- timestamp and container metadata. Prose stays prose: this filter removes only
-- redundant decoration and does not infer structured fields from sentences.

local LEVELS = {
    Debug = "debug",
    Error = "error",
    Notice = "notice",
    Warning = "warn",
}

local DISCARD = {
    "BOOT_ID",
    "CAP_EFFECTIVE",
    "CMDLINE",
    "CODE_FILE",
    "CODE_FUNC",
    "CODE_LINE",
    "COMM",
    "CONTAINER_ID_FULL",
    "CONTAINER_NAME",
    "CONTAINER_TAG",
    "EXE",
    "GID",
    "HOSTNAME",
    "MACHINE_ID",
    "PID",
    "PRIORITY",
    "SELINUX_CONTEXT",
    "SOURCE_REALTIME_TIMESTAMP",
    "SYSLOG_IDENTIFIER",
    "SYSTEMD_CGROUP",
    "SYSTEMD_INVOCATION_ID",
    "SYSTEMD_SLICE",
    "SYSTEMD_UNIT",
    "TRANSPORT",
    "UID",
}

function normalize_mosquitto(tag, ts, record)
    if tag ~= "svc.mosquitto.service" or record["CONTAINER_TAG"] == nil then
        return 0, ts, record
    end

    local message = record["log"]
    if type(message) ~= "string" then
        record["parser_status"] = "failed"
        record["parse_error"] = "mosquitto_message"
        record["_level"] = "warn"
        return 1, ts, record
    end

    message = message:gsub("[\r\n]+$", "")
    message = message:match("^%d+: (.*)$") or message

    local prefix, body = message:match("^(%a+): (.*)$")
    if prefix and LEVELS[prefix] then
        record["_level"] = LEVELS[prefix]
        message = body
    end

    record["log"] = message

    -- These journal/conmon fields duplicate values the final shaper derives
    -- from the event tag and host stamp. Retain only CONTAINER_ID as useful
    -- instance metadata; identifier is intentionally omitted because it is the
    -- same value as service for container stdout.
    for _, key in ipairs(DISCARD) do
        record[key] = nil
    end

    return 1, ts, record
end
