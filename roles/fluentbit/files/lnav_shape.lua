-- Shape the final Fluent Bit record for the local lnav JSONL store.
--
-- The preceding filters normalize MESSAGE into record.log, stamp the inventory
-- host, and infer the text level into the temp field record._level. This final
-- pass promotes the useful fields into the JSONL body (level, message, ...) and
-- tucks the original source metadata under fields.
--
-- Uses the classic 3-argument lua-filter signature (tag, timestamp, record).
-- See level_from_message.lua for why the metadata-aware prototype is avoided.

local function scrub(value)
    if type(value) ~= "string" then
        return value
    end
    return value:gsub("[%c]", " ")
end

local function scrub_message(value)
    if type(value) ~= "string" then
        return value
    end
    -- JSON Lines escapes internal LF inside the encoded string, so preserving
    -- it keeps a traceback in one physical JSONL record while lnav can render
    -- its frames on separate lines. Strip the other controls that can corrupt
    -- a terminal, then discard whitespace that carries no message content.
    return value:gsub("\r\n", "\n"):gsub("\r", "\n"):gsub("[%z\1-\9\11\12\14-\31\127]", " "):gsub("[ \t\n]+$", "")
end

local function service_from_tag(tag, record)
    local svc
    if string.sub(tag, 1, 4) == "svc." then
        if string.sub(tag, 1, 18) == "svc.libpod-conmon-" then
            svc = "podman_unnamed"
        else
            local sysid = record["SYSLOG_IDENTIFIER"]
            if record["CONTAINER_TAG"] == nil and type(sysid) == "string" and sysid ~= "" then
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
    BOOT_ID = true,
    CAP_EFFECTIVE = true,
    CMDLINE = true,
    CODE_FILE = true,
    CODE_FUNC = true,
    CODE_LINE = true,
    COMM = true,
    CONTAINER_ID_FULL = true,
    CONTAINER_NAME = true,
    CONTAINER_TAG = true,
    EXE = true,
    GID = true,
    HOSTNAME = true,
    MACHINE_ID = true,
    PID = true,
    PRIORITY = true,
    SELINUX_CONTEXT = true,
    SOURCE_REALTIME_TIMESTAMP = true,
    SYSLOG_IDENTIFIER = true,
    SYSTEMD_CGROUP = true,
    SYSTEMD_INVOCATION_ID = true,
    SYSTEMD_SLICE = true,
    SYSTEMD_UNIT = true,
    TRANSPORT = true,
    UID = true,
    host = true,
    log = true,
    severity_text = true,
    _level = true,
    parse_error = true,
}

function shape_lnav(tag, ts, record)
    local unit = record["SYSTEMD_UNIT"] or record["UNIT"] or unit_from_tag(tag)
    local identifier = record["SYSLOG_IDENTIFIER"]
    local service = service_from_tag(tag, record)
    local stream = stream_from_tag(tag)
    local level = record["_level"] or "info"

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
    -- redirects, favicon 404s, scanner probes -- stay at the pinned debug level:
    -- they are normal traffic on these reverse-proxy vhosts and would swamp the
    -- error stream. Done here rather than in a modify filter so the comparison
    -- is a plain numeric test on the typed value, not a regex on its rendering.
    local status = record["status"]
    if tag == "nginx.access" and type(status) == "number" and status >= 500 then
        level = "error"
    end

    -- The nginx access parser always produces status. Surface a missing status
    -- as a warning instead of letting a drifting parser masquerade as a plain
    -- message; Preserve_Key keeps the raw line under log.
    local parse_error = record["parse_error"]
    if parse_error == nil and tag == "nginx.access" and record["status"] == nil then
        parse_error = "nginx_access_custom"
        level = "warn"
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
        message = scrub_message(record["log"] or ""),
        stream = scrub(stream),
        fields = fields,
    }

    -- Only present on parse failures so a clean record stays one line in lnav
    -- (the format auto-expands the key onto its own row only when it exists).
    if parse_error ~= nil then
        shaped.parse_error = parse_error
    end

    return 1, ts, shaped
end
