-- Unit tests for conmon_partial_message.lua. Run via
-- `mise run test:fluentbit-lua` or directly with lua5.4.

local here = arg[0]:match("^(.*/)") or "./"
dofile(here .. "conmon_partial_message.lua")

local function check(label, got, want)
    assert(got == want, string.format("%s: got %s, want %s", label, tostring(got), tostring(want)))
end

local container_id = string.rep("1", 64)

do
    local first = {
        MESSAGE = '{"formattedMes',
        CONTAINER_ID_FULL = container_id,
        CONTAINER_PARTIAL_MESSAGE = "true",
        PRIORITY = "6",
    }
    local middle = {
        MESSAGE = 'sage":"split-',
        CONTAINER_ID_FULL = container_id,
        CONTAINER_PARTIAL_MESSAGE = "true",
        PRIORITY = "6",
    }
    local final = {
        MESSAGE = 'message"}',
        CONTAINER_ID_FULL = container_id,
        PRIORITY = "6",
    }

    local first_code = reassemble_conmon_message("svc.nexus.service", 1, first)
    local middle_code = reassemble_conmon_message("svc.nexus.service", 2, middle)
    local final_code = reassemble_conmon_message("svc.nexus.service", 3, final)

    check("split.first_dropped", first_code, -1)
    check("split.middle_dropped", middle_code, -1)
    check("split.final_emitted", final_code, 1)
    check("split.message", final.MESSAGE, '{"formattedMessage":"split-message"}')
    check("split.marker_removed", final.CONTAINER_PARTIAL_MESSAGE, nil)
end

do
    local record = {
        MESSAGE = "ordinary message",
        CONTAINER_ID_FULL = container_id,
        PRIORITY = "6",
    }
    local code = reassemble_conmon_message("svc.nexus.service", 4, record)
    check("ordinary.code", code, 0)
    check("ordinary.message", record.MESSAGE, "ordinary message")
end

do
    local record = {
        MESSAGE = "native journal message",
        PRIORITY = "6",
    }
    local code = reassemble_conmon_message("svc.nexus.service", 5, record)
    check("native.code", code, 0)
    check("native.message", record.MESSAGE, "native journal message")
end
