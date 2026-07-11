-- Normalize Headscale's zerolog JSON after the built-in parser has merged its
-- keys into the journald record. Preserve the original `log` value until the
-- JSON shape is validated so malformed and legacy output stays searchable.

local LEVELS = {
    trace = "trace",
    debug = "debug",
    info = "info",
    warn = "warn",
    error = "error",
    fatal = "fatal",
    panic = "fatal",
}

local DISCARD = {
    "time",
    "level",
    "message",
    -- Journald/conmon envelope fields duplicated by the final shaper or its
    -- tag-derived service/unit fields. Keep only the short container id as
    -- useful instance metadata; identifier is redundant with service here.
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
    "RUNTIME_SCOPE",
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

function normalize_headscale(tag, ts, record)
    if tag ~= "svc.headscale.service" or record["CONTAINER_TAG"] == nil then
        return 0, ts, record
    end

    local raw = record["log"]
    if record["level"] == nil and record["message"] == nil and record["time"] == nil then
        if looks_like_json(raw) then
            mark_failure(record, "headscale_json")
        else
            record["parser_status"] = "skipped"
            record["parser_reason"] = "non_json"
        end
        return 1, ts, record
    end

    if type(record["time"]) ~= "number" or type(record["level"]) ~= "string" or type(record["message"]) ~= "string" then
        mark_failure(record, "headscale_schema")
        return 1, ts, record
    end

    local level = LEVELS[record["level"]]
    if level == nil then
        mark_failure(record, "headscale_level")
        return 1, ts, record
    end

    record["log"] = record["message"]:gsub("[\r\n]+$", "")
    record["_level"] = level
    record["parser_status"] = "parsed"

    for _, key in ipairs(DISCARD) do
        record[key] = nil
    end

    return 1, ts, record
end
