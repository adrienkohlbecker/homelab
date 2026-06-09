-- Strip ANSI escape sequences (SGR colour codes, cursor moves) from the log
-- message. Some services ship raw escape bytes to journald: seerr (jellyseerr)
-- wraps its level token in colour codes (\x1b[34mdebug\x1b[39m), and pihole's
-- gravity cron writes cursor-move sequences. Beyond the visual noise in lnav,
-- the colour bytes hug the level word, so level_from_message's word-boundary
-- keyword scan cannot match a colourised "debug"/"warn" and misclassifies it
-- as info. This runs before level_from_message so both the classification and
-- the stored message see clean text.
--
-- Reads/writes record["log"] (the message key after the MESSAGE->log rename).
-- Returns code 0 (unchanged) for the common no-escape case so Fluent Bit does
-- not needlessly re-stamp every record. Classic 3-argument lua-filter
-- signature -- see level_from_message.lua for why the metadata-aware prototype
-- is avoided.

-- ESC [ <params> <final byte>: covers colours (m) and cursor moves (A-Za-z).
local CSI = "\27%[[0-9;?]*[A-Za-z]"

function strip_ansi(_tag, ts, record)
    local msg = record["log"]
    if type(msg) ~= "string" then
        return 0, ts, record
    end
    local cleaned, n = msg:gsub(CSI, "")
    if n == 0 then
        return 0, ts, record
    end
    record["log"] = cleaned
    return 1, ts, record
end
