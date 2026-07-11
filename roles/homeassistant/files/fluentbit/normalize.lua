-- Normalize Home Assistant's timestamp/level/thread/source header after the
-- regex parser has extracted it. Traceback continuations remain plain text.

local LEVELS = {
    DEBUG = "debug",
    INFO = "info",
    WARNING = "warn",
    ERROR = "error",
    CRITICAL = "critical",
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

local PARSER_FIELDS = {
    "ha_level",
    "ha_message",
    "ha_source",
    "ha_thread",
    "ha_timestamp",
}

local function discard_parser_fields(record)
    local changed = false
    for _, key in ipairs(PARSER_FIELDS) do
        if record[key] ~= nil then
            record[key] = nil
            changed = true
        end
    end
    return changed
end

local function discard_envelope(record)
    for _, key in ipairs(DISCARD) do
        record[key] = nil
    end
    discard_parser_fields(record)
end

local function looks_like_homeassistant_header(value)
    return type(value) == "string" and value:match("^%d%d%d%d%-%d%d%-%d%d ") ~= nil
end

local function mark_failure(record, reason)
    record["parser_status"] = "failed"
    record["parse_error"] = reason
    record["_level"] = "warn"
end

function normalize_homeassistant(tag, ts, record)
    if tag ~= "svc.homeassistant.service" then
        return 0, ts, record
    end
    if record["CONTAINER_TAG"] == nil then
        local changed = discard_parser_fields(record)
        return changed and 1 or 0, ts, record
    end

    local raw = record["log"]
    if type(raw) ~= "string" then
        mark_failure(record, "homeassistant_message")
        discard_envelope(record)
        return 1, ts, record
    end

    if record["ha_message"] == nil then
        if looks_like_homeassistant_header(raw) then
            mark_failure(record, "homeassistant_text")
        else
            record["parser_status"] = "skipped"
            record["parser_reason"] = "continuation"
        end
        record["log"] = raw:gsub("[\r\n]+$", "")
        discard_envelope(record)
        return 1, ts, record
    end

    local level = LEVELS[record["ha_level"]]
    if level == nil or type(record["ha_thread"]) ~= "string" or type(record["ha_source"]) ~= "string" then
        mark_failure(record, "homeassistant_schema")
        discard_envelope(record)
        return 1, ts, record
    end

    record["log"] = record["ha_message"]:gsub("[\r\n]+$", "")
    record["_level"] = level
    record["thread"] = record["ha_thread"]
    record["source"] = record["ha_source"]
    record["parser_status"] = "parsed"
    discard_envelope(record)

    return 1, ts, record
end
