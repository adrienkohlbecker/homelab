-- Reassemble conmon journald chunks before service-specific parsers run.
-- Conmon marks every non-final 8 KiB chunk with CONTAINER_PARTIAL_MESSAGE;
-- the final chunk has no marker and immediately follows the preceding chunks.

local buffers = {}
local max_message_bytes = 1024 * 1024

function reassemble_conmon_message(_tag, timestamp, record)
    local message = record["MESSAGE"]
    local container_id = record["CONTAINER_ID_FULL"]

    if type(message) ~= "string" or type(container_id) ~= "string" then
        return 0, timestamp, record
    end

    local key = container_id .. ":" .. tostring(record["PRIORITY"] or "")
    local buffered = buffers[key]
    local is_partial = record["CONTAINER_PARTIAL_MESSAGE"] == "true"

    if buffered ~= nil then
        local combined = buffered .. message
        if #combined > max_message_bytes then
            buffers[key] = nil
            return 0, timestamp, record
        end
        if is_partial then
            buffers[key] = combined
            return -1, timestamp, record
        end

        buffers[key] = nil
        record["MESSAGE"] = combined
        record["CONTAINER_PARTIAL_MESSAGE"] = nil
        return 1, timestamp, record
    end

    if is_partial then
        buffers[key] = message
        return -1, timestamp, record
    end

    return 0, timestamp, record
end
