-- Normalize Zigbee2MQTT's Winston JSON after Fluent Bit has merged its fields
-- into the journal record. The raw log remains available for parse failures.

local LEVELS = {
    debug = "debug",
    error = "error",
    info = "info",
    warning = "warn",
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

local function looks_like_json(value)
    return type(value) == "string" and value:match("^%s*{") ~= nil
end

local function clean_envelope(record)
    for _, key in ipairs(DISCARD) do
        record[key] = nil
    end
end

local function mark_failure(record, reason)
    record["parser_status"] = "failed"
    record["parse_error"] = reason
    record["_level"] = "warn"
    clean_envelope(record)
end

function normalize_z2m(tag, ts, record)
    if tag ~= "svc.z2m.service" or record["CONTAINER_TAG"] == nil then
        return 0, ts, record
    end

    local raw = record["log"]
    if record["level"] == nil and record["message"] == nil and record["timestamp"] == nil then
        if looks_like_json(raw) then
            mark_failure(record, "z2m_json")
        else
            record["log"] = type(raw) == "string" and raw:gsub("[\r\n]+$", "") or raw
            record["parser_status"] = "skipped"
            record["parser_reason"] = "non_json"
            clean_envelope(record)
        end
        return 1, ts, record
    end

    if
        type(record["level"]) ~= "string"
        or type(record["message"]) ~= "string"
        or type(record["timestamp"]) ~= "string"
    then
        mark_failure(record, "z2m_schema")
        return 1, ts, record
    end

    local level = LEVELS[record["level"]]
    if level == nil then
        mark_failure(record, "z2m_level")
        return 1, ts, record
    end

    local namespace, message = record["message"]:match("^([%w_:-]+):%s+(.*)$")
    if namespace == nil then
        mark_failure(record, "z2m_namespace")
        return 1, ts, record
    end

    record["log"] = message:gsub("[\r\n]+$", "")
    record["_level"] = level
    record["namespace"] = namespace
    record["parser_status"] = "parsed"
    if type(record["stack"]) == "string" then
        record["exception"] = record["stack"]
    end

    record["level"] = nil
    record["message"] = nil
    record["timestamp"] = nil
    record["stack"] = nil
    clean_envelope(record)

    return 1, ts, record
end
