-- Normalize Bazarr's plain-text console grammar after the regex parser has
-- extracted its explicit metadata. Linuxserver init output remains prose.

local LEVELS = {
    DEBUG = "debug",
    INFO = "info",
    WARNING = "warn",
    ERROR = "error",
    CRITICAL = "fatal",
}

local DISCARD = {
    "bazarr_time",
    "bazarr_level",
    "message",
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

local function trim_line_ending(value)
    return value:gsub("[\r\n]+$", "")
end

local function looks_like_bazarr(message)
    return message:match("^%d%d%d%d%-%d%d%-%d%d %d%d:%d%d:%d%d,") ~= nil
end

local function discard_envelope(record)
    for _, key in ipairs(DISCARD) do
        record[key] = nil
    end
end

local function mark_failure(record, reason)
    record["parser_status"] = "failed"
    record["parse_error"] = reason
    record["_level"] = "warn"
end

function normalize_bazarr(tag, ts, record)
    if tag ~= "svc.bazarr.service" or record["CONTAINER_TAG"] == nil then
        return 0, ts, record
    end

    local raw = record["log"]
    if type(raw) ~= "string" then
        mark_failure(record, "bazarr_message")
        discard_envelope(record)
        return 1, ts, record
    end

    raw = trim_line_ending(raw)
    if record["bazarr_time"] == nil then
        record["log"] = raw
        if looks_like_bazarr(raw) then
            mark_failure(record, "bazarr_console")
        else
            record["parser_status"] = "skipped"
            record["parser_reason"] = "non_bazarr"
        end
        discard_envelope(record)
        return 1, ts, record
    end

    local level = LEVELS[record["bazarr_level"]]
    if
        type(record["message"]) ~= "string"
        or type(record["logger"]) ~= "string"
        or type(record["thread_id"]) ~= "string"
        or type(record["source"]) ~= "string"
        or type(record["source_line"]) ~= "number"
    then
        mark_failure(record, "bazarr_schema")
    elseif level == nil then
        mark_failure(record, "bazarr_level")
    else
        record["log"] = trim_line_ending(record["message"])
        record["_level"] = level
        record["parser_status"] = "parsed"
    end

    discard_envelope(record)
    return 1, ts, record
end
