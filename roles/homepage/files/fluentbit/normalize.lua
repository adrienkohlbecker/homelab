-- Normalize Homepage's Winston console grammar after the regex parser has
-- extracted its explicit timestamp, severity, source label, and message.

local LEVELS = {
    error = "error",
    warn = "warn",
    info = "info",
    http = "info",
    verbose = "debug",
    debug = "debug",
    silly = "trace",
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
    "CONTAINER_PARTIAL_MESSAGE",
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
    "homepage_level",
    "homepage_message",
    "homepage_source",
    "homepage_timestamp",
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

local function trim_line_ending(value)
    return value:gsub("[\r\n]+$", "")
end

local function looks_like_homepage_header(value)
    return type(value) == "string" and value:match("^%[[^]]+%] ") ~= nil
end

local function mark_failure(record, reason)
    record["parser_status"] = "failed"
    record["parse_error"] = reason
    record["_level"] = "warn"
end

function normalize_homepage(tag, ts, record)
    if tag ~= "svc.homepage.service" then
        return 0, ts, record
    end
    if record["CONTAINER_TAG"] == nil then
        local changed = discard_parser_fields(record)
        return changed and 1 or 0, ts, record
    end

    local raw = record["log"]
    if type(raw) ~= "string" then
        mark_failure(record, "homepage_message")
        discard_envelope(record)
        return 1, ts, record
    end

    if record["homepage_timestamp"] == nil then
        record["log"] = trim_line_ending(raw)
        if looks_like_homepage_header(raw) then
            mark_failure(record, "homepage_winston")
        else
            record["parser_status"] = "skipped"
            record["parser_reason"] = "non_winston"
        end
        discard_envelope(record)
        return 1, ts, record
    end

    local level = LEVELS[record["homepage_level"]]
    if
        level == nil
        or type(record["homepage_timestamp"]) ~= "string"
        or type(record["homepage_message"]) ~= "string"
        or (record["homepage_source"] ~= nil and type(record["homepage_source"]) ~= "string")
    then
        mark_failure(record, "homepage_schema")
        discard_envelope(record)
        return 1, ts, record
    end

    record["log"] = trim_line_ending(record["homepage_message"])
    record["_level"] = level
    record["parser_status"] = "parsed"
    if record["homepage_source"] ~= nil then
        record["source"] = record["homepage_source"]
    end
    discard_envelope(record)

    return 1, ts, record
end
