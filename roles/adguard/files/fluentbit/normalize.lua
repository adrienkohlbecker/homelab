-- Normalize AdGuard Home's text envelope and logfmt attributes after the
-- regex parser has split the timestamp, level, and body. The original journal
-- record remains authoritative when parsing fails.

local LEVELS = {
    debug = "debug",
    info = "info",
    warn = "warn",
    warning = "warn",
    error = "error",
    critical = "critical",
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
    "adguard_body",
    "adguard_level",
    "adguard_timestamp",
}

local RESERVED = {
    _level = true,
    fields = true,
    host = true,
    log = true,
    parse_error = true,
    parser_reason = true,
    parser_status = true,
    service = true,
    severity_text = true,
    source = true,
    stream = true,
    time = true,
    unit = true,
}

local function discard_envelope(record)
    for _, key in ipairs(DISCARD) do
        record[key] = nil
    end
end

local function looks_like_adguard(value)
    return type(value) == "string" and value:match("^%d%d%d%d/%d%d/%d%d ") ~= nil
end

local function decode_quoted(text, position)
    local value = {}
    local escaped = false
    position = position + 1

    while position <= #text do
        local character = text:sub(position, position)
        if escaped then
            local escapes = { n = "\n", r = "\r", t = "\t" }
            table.insert(value, escapes[character] or character)
            escaped = false
        elseif character == "\\" then
            escaped = true
        elseif character == '"' then
            return table.concat(value), position + 1
        else
            table.insert(value, character)
        end
        position = position + 1
    end

    return nil, position
end

local function parse_attributes(text)
    local boundary = text:find(" [a-z][a-z0-9_]*=")
    if boundary == nil then
        return text, {}
    end

    local message = text:sub(1, boundary - 1)
    local attributes = {}
    local position = boundary + 1

    while position <= #text do
        local key_start, key_end, key = text:find("([a-z][a-z0-9_]*)=", position)
        if key_start ~= position then
            return nil, nil
        end

        position = key_end + 1
        local value
        if text:sub(position, position) == '"' then
            value, position = decode_quoted(text, position)
            if value == nil then
                return nil, nil
            end
        else
            local space = text:find(" ", position, true)
            if space == nil then
                value = text:sub(position)
                position = #text + 1
            else
                value = text:sub(position, space - 1)
                position = space
            end
            if value == "" then
                return nil, nil
            end
        end

        if value == "true" then
            value = true
        elseif value == "false" then
            value = false
        else
            value = tonumber(value) or value
        end
        attributes[key] = value

        while text:sub(position, position) == " " do
            position = position + 1
        end
    end

    return message, attributes
end

local function mark_failure(record, reason)
    record["parser_status"] = "failed"
    record["parse_error"] = reason
    record["_level"] = "warn"
end

function normalize_adguard(tag, ts, record)
    if tag ~= "svc.adguard.service" or record["CONTAINER_TAG"] == nil then
        return 0, ts, record
    end

    local raw = record["log"]
    local body = record["adguard_body"]
    if type(raw) ~= "string" then
        mark_failure(record, "adguard_message")
        discard_envelope(record)
        return 1, ts, record
    end

    if type(body) ~= "string" then
        if looks_like_adguard(raw) then
            mark_failure(record, "adguard_text")
        else
            record["parser_status"] = "skipped"
            record["parser_reason"] = "non_adguard"
        end
        record["log"] = raw:gsub("[\r\n]+$", "")
        discard_envelope(record)
        return 1, ts, record
    end

    local level = LEVELS[record["adguard_level"]]
    if level == nil then
        mark_failure(record, "adguard_level")
        discard_envelope(record)
        return 1, ts, record
    end

    body = body:gsub("[\r\n]+$", "")
    local source, message = body:match("^([a-z][a-z0-9_]*):%s+(.*)$")
    if source == nil then
        message = body
    end

    local attributes
    message, attributes = parse_attributes(message)
    if message == nil then
        mark_failure(record, "adguard_logfmt")
        discard_envelope(record)
        return 1, ts, record
    end

    record["log"] = message
    record["_level"] = level
    record["parser_status"] = "parsed"
    if source ~= nil then
        record["source"] = source
    end
    for key, value in pairs(attributes) do
        record[RESERVED[key] and ("adguard_" .. key) or key] = value
    end
    discard_envelope(record)

    return 1, ts, record
end
