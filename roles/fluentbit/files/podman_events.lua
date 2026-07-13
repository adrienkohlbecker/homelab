-- Normalize Podman's structured journald events before the common lnav
-- shaper runs. Podman already supplies authoritative PODMAN_* fields, so the
-- human MESSAGE is a redundant rendering that repeats the timestamp, full
-- object id, image metadata, and every image label. Build a compact summary
-- from those fields and retain the useful event data under stable names.

local function nonempty(value)
    if type(value) == "string" and value ~= "" then
        return value
    end
    return nil
end

local function move(record, source, destination, convert_number)
    local value = nonempty(record[source])
    record[source] = nil
    if value == nil then
        return nil
    end

    if convert_number then
        value = tonumber(value) or value
    end
    record[destination] = value
    return value
end

function normalize_podman_event(_tag, ts, record)
    if record["SYSLOG_IDENTIFIER"] ~= "podman" then
        return 0, ts, record
    end

    local event = nonempty(record["PODMAN_EVENT"])
    local object_type = nonempty(record["PODMAN_TYPE"])
    if event == nil or object_type == nil then
        return 0, ts, record
    end

    event = move(record, "PODMAN_EVENT", "event")
    object_type = move(record, "PODMAN_TYPE", "object_type")
    local name = move(record, "PODMAN_NAME", "name")
    local image = move(record, "PODMAN_IMAGE", "image")
    local object_id = move(record, "PODMAN_ID", "object_id")
    local exit_code = move(record, "PODMAN_EXIT_CODE", "exit_code", true)
    local health_status = move(record, "PODMAN_HEALTH_STATUS", "health_status")

    -- PODMAN_TIME duplicates the journal timestamp. PODMAN_LABELS is a JSON
    -- rendering of image labels already repeated in the original MESSAGE; it
    -- can be several kilobytes and has no operational value in the aggregate.
    record["PODMAN_TIME"] = nil
    record["PODMAN_LABELS"] = nil

    local subject = name or image
    if subject == nil and object_id ~= nil then
        subject = string.sub(object_id, 1, 12)
    end

    local message = object_type .. " " .. event
    if subject ~= nil then
        message = message .. " " .. subject
    end

    local details = {}
    if health_status ~= nil then
        table.insert(details, "health=" .. health_status)
    end
    if exit_code ~= nil then
        table.insert(details, "exit=" .. tostring(exit_code))
    end
    if #details > 0 then
        message = message .. " (" .. table.concat(details, ", ") .. ")"
    end

    record["MESSAGE"] = message
    record["parser_status"] = "parsed"
    return 1, ts, record
end
