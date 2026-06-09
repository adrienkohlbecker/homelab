-- Shape the final Fluent Bit record for the local lnav JSONL store.
--
-- The preceding filters normalize MESSAGE into record.log, stamp the
-- inventory host, and infer severity into metadata.otlp.severity_*.
-- stdout/json_lines serializes only the record body, so this final pass
-- promotes the useful fields back into the body and tucks the original
-- source metadata under fields.

local function scrub(value)
    if type(value) ~= "string" then
        return value
    end
    return value:gsub("[%c]", " ")
end

local function service_from_tag(tag, record)
    local svc
    if string.sub(tag, 1, 4) == "svc." then
        if string.sub(tag, 1, 18) == "svc.libpod-conmon-" then
            svc = "podman_unnamed"
        else
            local sysid = record["SYSLOG_IDENTIFIER"]
            if type(sysid) == "string" and sysid ~= "" then
                if sysid:find("(mitogen:", 1, true) then
                    svc = "mitogen"
                else
                    svc = sysid
                end
            end
            if svc == nil or svc == "" then
                svc = string.sub(tag, 5):gsub("%.service$", "")
            end
        end
    elseif string.sub(tag, 1, 6) == "nginx." then
        local sub = string.sub(tag, 7)
        if sub ~= "" then
            svc = "nginx_" .. sub
        end
    else
        svc = tag
    end
    if svc == nil or svc == "" then
        svc = "unknown"
    end
    return string.lower(svc)
end

local function stream_from_tag(tag)
    if string.sub(tag, 1, 4) == "svc." then
        return "journald"
    elseif string.sub(tag, 1, 6) == "nginx." then
        return "nginx"
    end
    return tag
end

local function unit_from_tag(tag)
    if string.sub(tag, 1, 4) == "svc." then
        local unit = string.sub(tag, 5)
        if unit ~= "" then
            return unit
        end
    end
    return nil
end

local EXCLUDE_FROM_FIELDS = {
    host = true,
    log = true,
    severity_text = true,
    severity_number = true,
}

-- pihole-FTL's own level tokens -> {lnav level, OTLP severity number}. NOTICE
-- maps to info (matching level_from_message's treatment); DEBUG_<category>
-- variants are handled by prefix below. Anything unrecognised falls back to
-- info so an FTL record always has a predictable level.
local FTL_LEVEL = {
    FATAL = { "fatal", 21 },
    CRIT = { "fatal", 21 },
    ERR = { "error", 17 },
    ERROR = { "error", 17 },
    WARNING = { "warn", 13 },
    WARN = { "warn", 13 },
    INFO = { "info", 9 },
    NOTICE = { "info", 9 },
    DEBUG = { "debug", 5 },
}

function shape_lnav(tag, ts, _group, metadata, record)
    metadata.otlp = metadata.otlp or {}

    local unit = record["SYSTEMD_UNIT"] or record["UNIT"] or unit_from_tag(tag)
    local identifier = record["SYSLOG_IDENTIFIER"]
    local service = service_from_tag(tag, record)
    local level = metadata.otlp.severity_text or "info"

    local healthcheck_unit = record["UNIT"]
    if type(healthcheck_unit) == "string" then
        local cid = healthcheck_unit:match("^([0-9a-f]+)%.service$")
        if cid and #cid == 64 then
            service = "podman_healthcheck"
            record["CONTAINER_ID_FULL"] = cid
            record["CONTAINER_ID"] = string.sub(cid, 1, 12)
        end
    end

    -- A 5xx in an nginx access record is an upstream/proxy failure: promote it
    -- to error level so lnav's error navigation lands on it. The status field
    -- is set by the nginx_access_custom parser (typed integer). 4xx -- auth
    -- redirects, favicon 404s, scanner probes -- stay at the pinned info level:
    -- they are normal traffic on these reverse-proxy vhosts and would swamp the
    -- error stream. Done here rather than in a modify filter so the comparison
    -- is a plain numeric test on the typed value, not a regex on its rendering.
    local status = record["status"]
    if tag == "nginx.access" and type(status) == "number" and status >= 500 then
        level = "error"
        metadata.otlp.severity_number = 17
    end

    -- pihole-FTL carries its own authoritative level in the prefix (extracted
    -- as ftl_level by the parser). Map it directly instead of trusting the
    -- generic keyword scan, which would misread a body like "... SQL error ..."
    -- on an INFO line. Promote the parsed body to the message (via log, which
    -- the shaper already reads) so the redundant timestamp/level/pid prefix is
    -- not repeated under lnav's line-format; a non-matching line has no body
    -- and keeps its raw log line as the message.
    if tag == "pihole_ftl" then
        local ftl_level = record["ftl_level"]
        if type(ftl_level) == "string" then
            local mapped = FTL_LEVEL[ftl_level]
            if not mapped and string.sub(ftl_level, 1, 5) == "DEBUG" then
                mapped = FTL_LEVEL.DEBUG
            end
            if mapped then
                level = mapped[1]
                metadata.otlp.severity_number = mapped[2]
            end
        end
        local body = record["body"]
        if type(body) == "string" then
            record["log"] = body
            record["body"] = nil
        end
    end

    local fields = {}
    for k, v in pairs(record) do
        if not EXCLUDE_FROM_FIELDS[k] then
            fields[k] = scrub(v)
        end
    end

    local shaped = {
        host = scrub(record["host"]),
        service = scrub(service),
        unit = scrub(unit),
        identifier = scrub(identifier),
        level = scrub(level),
        level_number = tonumber(metadata.otlp.severity_number or 9) or 9,
        message = scrub(record["log"] or ""),
        stream = scrub(stream_from_tag(tag)),
        fields = fields,
    }

    return 1, ts, metadata, shaped
end
