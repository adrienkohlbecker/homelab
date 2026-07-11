-- Unit tests for AdGuard Home's post-regex Fluent Bit normalizer. Input
-- records model the parser filter's Reserve_Data/Preserve_Key output.

local here = arg[0]:match("^(.*/)") or "./"
dofile(here .. "normalize.lua")

local failures = 0

local function check(label, got, want)
    if got ~= want then
        failures = failures + 1
        print(string.format("FAIL  %s\n        got:  %s\n        want: %s", label, tostring(got), tostring(want)))
    end
end

local function normalize(record, tag)
    local code = normalize_adguard(tag or "svc.adguard.service", 0, record)
    return record, code
end

do
    local rec, code = normalize({
        log = "2026/07/11 12:00:00.123456 [info] filtering: filter updated id=1 bytes_written=12345 rules_count=67890\n",
        CONTAINER_TAG = "adguard",
        CONTAINER_ID = "123456789abc",
        CONTAINER_ID_FULL = "123456789abcdef0",
        CMDLINE = "/usr/bin/conmon --log-tag adguard",
        PRIORITY = "3",
        SYSTEMD_UNIT = "adguard.service",
        SYSLOG_IDENTIFIER = "adguard",
        adguard_timestamp = "2026/07/11 12:00:00.123456",
        adguard_level = "info",
        adguard_body = "filtering: filter updated id=1 bytes_written=12345 rules_count=67890\n",
    })
    check("filter.code", code, 1)
    check("filter.message", rec.log, "filter updated")
    check("filter.level", rec._level, "info")
    check("filter.source", rec.source, "filtering")
    check("filter.id", rec.id, 1)
    check("filter.bytes", rec.bytes_written, 12345)
    check("filter.rules", rec.rules_count, 67890)
    check("filter.status", rec.parser_status, "parsed")
    check("filter.short_container_id_kept", rec.CONTAINER_ID, "123456789abc")
    check("filter.timestamp_removed", rec.adguard_timestamp, nil)
    check("filter.container_tag_removed", rec.CONTAINER_TAG, nil)
    check("filter.full_container_id_removed", rec.CONTAINER_ID_FULL, nil)
    check("filter.cmdline_removed", rec.CMDLINE, nil)
    check("filter.priority_removed", rec.PRIORITY, nil)
    check("filter.unit_removed", rec.SYSTEMD_UNIT, nil)
    check("filter.identifier_removed", rec.SYSLOG_IDENTIFIER, nil)
end

do
    local rec = normalize({
        log = '2026/07/11 12:01:00.123456 [error] dnsproxy: exchange failed upstream=https://cloudflare-dns.com:443/dns-query question=";example.com.\\tIN\\t A" duration=10.25s err="first line\\nsecond line"\n',
        CONTAINER_TAG = "adguard",
        adguard_timestamp = "2026/07/11 12:01:00.123456",
        adguard_level = "error",
        adguard_body = 'dnsproxy: exchange failed upstream=https://cloudflare-dns.com:443/dns-query question=";example.com.\\tIN\\t A" duration=10.25s err="first line\\nsecond line"\n',
    })
    check("error.message", rec.log, "exchange failed")
    check("error.level", rec._level, "error")
    check("error.source", rec.source, "dnsproxy")
    check("error.upstream", rec.upstream, "https://cloudflare-dns.com:443/dns-query")
    check("error.question", rec.question, ";example.com.\tIN\t A")
    check("error.duration", rec.duration, "10.25s")
    check("error.detail", rec.err, "first line\nsecond line")
end

do
    local rec = normalize({
        log = '2026/07/11 12:02:00.123456 [info] starting adguard home version="AdGuard Home, version v0.107.77"\n',
        CONTAINER_TAG = "adguard",
        adguard_timestamp = "2026/07/11 12:02:00.123456",
        adguard_level = "info",
        adguard_body = 'starting adguard home version="AdGuard Home, version v0.107.77"\n',
    })
    check("startup.message", rec.log, "starting adguard home")
    check("startup.source", rec.source, nil)
    check("startup.version", rec.version, "AdGuard Home, version v0.107.77")
end

do
    local rec = normalize({
        log = "2026/07/11 12:03:00.123456 [info] warning: no users in the configuration file; authentication is disabled\n",
        CONTAINER_TAG = "adguard",
        adguard_timestamp = "2026/07/11 12:03:00.123456",
        adguard_level = "info",
        adguard_body = "warning: no users in the configuration file; authentication is disabled\n",
    })
    check("warning.message", rec.log, "no users in the configuration file; authentication is disabled")
    check("warning.level", rec._level, "info")
    check("warning.source", rec.source, "warning")
end

do
    local rec = normalize({
        log = "2026/07/11 malformed AdGuard header",
        CONTAINER_TAG = "adguard",
    })
    check("malformed.status", rec.parser_status, "failed")
    check("malformed.error", rec.parse_error, "adguard_text")
    check("malformed.level", rec._level, "warn")
    check("malformed.raw", rec.log, "2026/07/11 malformed AdGuard header")
end

do
    local rec = normalize({
        log = '2026/07/11 12:04:00.123456 [error] dnsproxy: exchange failed err="unterminated',
        CONTAINER_TAG = "adguard",
        adguard_timestamp = "2026/07/11 12:04:00.123456",
        adguard_level = "error",
        adguard_body = 'dnsproxy: exchange failed err="unterminated',
    })
    check("logfmt.status", rec.parser_status, "failed")
    check("logfmt.error", rec.parse_error, "adguard_logfmt")
    check("logfmt.raw", rec.log, '2026/07/11 12:04:00.123456 [error] dnsproxy: exchange failed err="unterminated')
end

do
    local rec = normalize({ log = "child output\n", CONTAINER_TAG = "adguard", PRIORITY = "3" })
    check("child.message", rec.log, "child output")
    check("child.status", rec.parser_status, "skipped")
    check("child.reason", rec.parser_reason, "non_adguard")
    check("child.priority_removed", rec.PRIORITY, nil)
end

do
    local record = { log = "podman command output" }
    local rec, code = normalize(record)
    check("unit_output.code", code, 0)
    check("unit_output.status", rec.parser_status, nil)
    check("unit_output.raw", rec.log, "podman command output")
end

do
    local rec, code = normalize({ log = "untouched", CONTAINER_TAG = "adguard" }, "svc.other.service")
    check("tag.code", code, 0)
    check("tag.container_tag", rec.CONTAINER_TAG, "adguard")
end

if failures > 0 then
    os.exit(1)
end
print("PASS  adguard normalize")
