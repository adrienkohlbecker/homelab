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
    if tag ~= "svc.jellyfin.service" then
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
