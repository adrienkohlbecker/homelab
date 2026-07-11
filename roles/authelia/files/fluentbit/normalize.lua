-- Normalize Authelia's native JSON stdout after the built-in parser has merged
-- its explicit application metadata into the journald record.

local LEVELS = {
    trace = "trace",
    debug = "debug",
    info = "info",
    notice = "notice",
    warn = "warn",
    warning = "warn",
    error = "error",
    fatal = "fatal",
}

local DISCARD = {
    "time",
    "level",
    "msg",
    -- Journald/conmon fields duplicated by the final shaper or its tag-derived
    -- service/unit fields. Keep only the short container id as useful instance
    -- metadata; identifier is redundant with service for container stdout.
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

local function looks_like_json(value)
    return type(value) == "string" and value:match("^%s*{") ~= nil
end

local function mark_failure(record, reason)
    record["parser_status"] = "failed"
    record["parse_error"] = reason
    record["_level"] = "warn"
end

function normalize_authelia(tag, ts, record)
    if tag ~= "svc.authelia.service" or record["CONTAINER_TAG"] == nil then
        return 0, ts, record
    end

    local raw = record["log"]
    if type(record["time"]) ~= "string" or type(record["level"]) ~= "string" or type(record["msg"]) ~= "string" then
        if looks_like_json(raw) and (record["time"] ~= nil or record["level"] ~= nil or record["msg"] ~= nil) then
            mark_failure(record, "authelia_schema")
        elseif looks_like_json(raw) then
            mark_failure(record, "authelia_json")
        else
            record["parser_status"] = "skipped"
            record["parser_reason"] = "non_json"
        end
        return 1, ts, record
    end

    local level = LEVELS[record["level"]:lower()]
    if level == nil then
        mark_failure(record, "authelia_level")
        return 1, ts, record
    end

    record["log"] = record["msg"]:gsub("[\r\n]+$", "")
    record["_level"] = level
    record["parser_status"] = "parsed"

    for _, key in ipairs(DISCARD) do
        record[key] = nil
    end

    return 1, ts, record
end
