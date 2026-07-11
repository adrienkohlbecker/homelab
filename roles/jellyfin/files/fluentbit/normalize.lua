-- Normalize Jellyfin's rendered Compact Log Event Format records after the
-- built-in JSON parser has merged them into the journald record. The original
-- `log` key is preserved until here so malformed JSON stays searchable.

local LEVELS = {
    Verbose = "trace",
    Debug = "debug",
    Information = "info",
    Warning = "warn",
    Error = "error",
    Fatal = "fatal",
}

local RENAMES = {
    SourceContext = "source",
    ThreadId = "thread_id",
    ["@x"] = "exception",
    ["@tr"] = "trace_id",
    ["@sp"] = "span_id",
    ["@i"] = "event_id",
}

local DISCARD = {
    "@t",
    "@m",
    "@mt",
    "@l",
    "@x",
    "@tr",
    "@sp",
    "@i",
    "@r",
    "JellyfinFormat",
    "SourceContext",
    "ThreadId",
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

function normalize_jellyfin(tag, ts, record)
    -- The systemd input tags every record from the unit identically. Podman's
    -- journald driver adds CONTAINER_TAG only to container stdout/stderr, so
    -- use its presence to leave podman command and unit lifecycle output alone.
    if tag ~= "svc.jellyfin.service" or record["CONTAINER_TAG"] == nil then
        return 0, ts, record
    end

    local raw = record["log"]
    if record["JellyfinFormat"] ~= "clef" then
        if looks_like_json(raw) then
            mark_failure(record, "jellyfin_json")
        else
            record["parser_status"] = "skipped"
            record["parser_reason"] = "non_json"
        end
        return 1, ts, record
    end

    if type(record["@t"]) ~= "string" or type(record["@m"]) ~= "string" then
        mark_failure(record, "jellyfin_schema")
        return 1, ts, record
    end

    local level_name = record["@l"] or "Information"
    local level = LEVELS[level_name]
    if level == nil then
        mark_failure(record, "jellyfin_level")
        return 1, ts, record
    end

    record["log"] = record["@m"]:gsub("[\r\n]+$", "")
    record["_level"] = level
    record["parser_status"] = "parsed"

    for source, destination in pairs(RENAMES) do
        if record[source] ~= nil then
            record[destination] = record[source]
        end
    end
    for _, key in ipairs(DISCARD) do
        record[key] = nil
    end

    return 1, ts, record
end
