-- Parse UniFi syslog (RFC 3164 UDP, Tag=unifi) into structured fields.
--
-- The UDM gateway + APs/switches forward three payload shapes, all with a
-- "<pri>Mon DD HH:MM:SS HOST " envelope:
--   1. Gateway daemon syslog (hostname repeated by the UDM):
--      <4>Jun  3 16:32:52 DreamMachinePro DreamMachinePro kernel: <msg>
--   2. AP/switch device syslog ("mac,model-version" program tag):
--      <30>Jun  3 .. AccessPointnanoHD e063da..,UAP-nanoHD-6.7.41+15623: mcad: <msg>
--   3. Gateway CEF events (client connect/roam/IPS/etc.):
--      <pri>.. DreamMachinePro CEF:0|Ubiquiti|UniFi Network|<ver>|<sig>|<name>|<sev>|<ext>
--
-- We deliberately do NOT adopt the syslog timestamp as the event time: the
-- RFC 3164 stamp carries no year and no timezone, and the devices send local
-- time (CEST). strptime would read it as UTC and land every record ~2h in the
-- future, hiding it from HyperDX's time window. These are live-forwarded logs,
-- so the ingestion timestamp (the `ts` returned unchanged) is within ms of the
-- event.
--
-- Severity comes from the syslog PRI (facility*8 + severity), more reliable
-- than scanning the body; we stamp metadata.otlp.severity_* here so the
-- downstream level_from_message.lua leaves it alone. The human-readable line
-- lands in record["log"] (LogRecord.Body); every other extracted field stays
-- on the record and otlp_shape.lua lifts it into LogAttributes.
--
-- Unit tests (real captured payloads): parse_unifi_test.lua, run via
-- `mise run test:fluentbit-lua`.

-- syslog severity (PRI % 8) -> OTLP severity_text/number, aligned with
-- level_from_message.lua's SEV table (crit and worse collapse to fatal).
local SYSLOG_SEV = {
    [0] = { "fatal", 21 },
    [1] = { "fatal", 21 },
    [2] = { "fatal", 21 },
    [3] = { "error", 17 },
    [4] = { "warn", 13 },
    [5] = { "info", 9 },
    [6] = { "info", 9 },
    [7] = { "debug", 5 },
}

-- Split a CEF extension blob ("k1=v1 k2=v2 ..." where values may contain
-- spaces) into key/value pairs. Keys are identifiers immediately followed by
-- '=' and preceded by start-of-string or a space; a value runs until the space
-- before the next such key (or end of string). UniFi keys are all "UNIFI<x>"
-- plus a trailing "msg", so a false key boundary inside a value is effectively
-- impossible.
local function split_cef_ext(ext)
    local marks = {}
    local i = 1
    while true do
        local s, e, key = ext:find("([%a][%w_%.]*)=", i)
        if not s then
            break
        end
        if s == 1 or ext:sub(s - 1, s - 1) == " " then
            marks[#marks + 1] = { s = s, key = key, vstart = e + 1 }
        end
        i = e + 1
    end
    local out = {}
    for idx, m in ipairs(marks) do
        local vend = marks[idx + 1] and (marks[idx + 1].s - 2) or #ext
        out[m.key] = ext:sub(m.vstart, vend)
    end
    return out
end

local function parse_cef(record, cef)
    -- CEF:Version|Vendor|Product|DeviceVersion|SignatureID|Name|Severity|Extension
    local ver, vendor, product, dver, sig, name, sev, ext = cef:match("^CEF:(.-)|(.-)|(.-)|(.-)|(.-)|(.-)|(.-)|(.*)$")
    if not ver then
        -- Malformed CEF: keep the raw blob as the body.
        record["log"] = cef
        return
    end
    record["unifi_vendor"] = vendor
    record["unifi_product"] = product
    record["unifi_cef_device_version"] = dver
    record["unifi_cef_signature_id"] = sig
    record["unifi_cef_name"] = name
    record["unifi_cef_severity"] = sev

    -- Default the body to the event name; the `msg` extension field (the
    -- device's own human sentence) overrides it when present.
    local body = name
    for k, v in pairs(split_cef_ext(ext or "")) do
        if k == "msg" then
            body = v
        elseif k:sub(1, 5) == "UNIFI" then
            record["unifi_" .. k:sub(6)] = v
        else
            record["unifi_" .. k] = v
        end
    end
    record["log"] = body
end

local function parse_device_syslog(record, host, tail)
    -- The UDM repeats the hostname as the first token of the message body;
    -- drop the duplicate so the program tag leads.
    local dedup = tail:match("^" .. host:gsub("(%W)", "%%%1") .. " (.*)$")
    if dedup then
        tail = dedup
    end

    -- "<program>: <message>" -- program is "kernel" / "systemd[1]" on the
    -- gateway, or the AP/switch "mac,model-version" tag.
    local program, msg = tail:match("^(.-): (.*)$")
    if not program then
        record["log"] = tail
        return
    end
    local mac, model = program:match("^(%x[%x]+),(.+)$")
    if mac then
        record["unifi_device_mac"] = mac
        record["unifi_device_model"] = model
    else
        record["unifi_program"] = program
    end
    record["log"] = msg
end

function parse_unifi(_tag, ts, _group, metadata, record)
    local line = record["log"]
    if type(line) ~= "string" then
        return 0, ts, metadata, record
    end

    local pri, rest = line:match("^<(%d+)>(.*)$")
    if pri then
        local sev = SYSLOG_SEV[tonumber(pri) % 8]
        if sev then
            metadata.otlp = metadata.otlp or {}
            metadata.otlp.severity_text = sev[1]
            metadata.otlp.severity_number = sev[2]
        end
    else
        rest = line
    end

    -- "Mon DD HH:MM:SS HOST <tail>" -- consume the timestamp (not adopted as
    -- event time; see header) and split host from the rest.
    local after = rest:match("^%a%a%a%s+%d+%s+[%d:]+%s+(.*)$")
    if not after then
        -- Unrecognised envelope; ship the line unmodified.
        return 1, ts, metadata, record
    end
    local host, tail = after:match("^(%S+)%s+(.*)$")
    if not host then
        return 1, ts, metadata, record
    end
    record["unifi_source"] = host

    local cef = tail:match("^(CEF:.*)$")
    if cef then
        parse_cef(record, cef)
    else
        parse_device_syslog(record, host, tail)
    end

    return 1, ts, metadata, record
end
