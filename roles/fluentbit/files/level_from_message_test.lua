-- Unit tests for level_from_message.lua. Run via `mise run test:fluentbit-lua`
-- (which locates a system lua); or directly: `lua5.4 level_from_message_test.lua`.
-- Exits non-zero on the first failed assertion.
--
-- The message samples are real lines captured from lab's journal
-- (journalctl -o json) across the services that actually run there, picked
-- to cover each LEVEL_RULES branch plus the deliberate quirks (notice->info,
-- LOG:->info, first-120-char scope, journal fallback). Keep them
-- verbatim -- they are the regression corpus.

local here = arg[0]:match("^(.*/)") or "./"
dofile(here .. "level_from_message.lua")

local function check(label, got, want)
    assert(got == want, string.format("%s: got %s, want %s", label, tostring(got), tostring(want)))
end

-- Run a log line through the filter (default tag svc.test.service, no extra
-- record fields) and return (level_text, return_code).
local function sev(line, tag, record)
    record = record or {}
    record.log = line
    local code = set_priority(tag or "svc.test.service", 0, record)
    return record["_level"], code
end

local message_cases = {
    {
        label = "docker.info.text",
        line = 'time="2026-06-03T17:40:42Z" level=info msg="Created exec session 8ac8367408b2 in container 768913f"',
        want = "info",
    },
    {
        label = "docker.warn.text",
        line = 'time="2026-06-03T17:40:47Z" level=warning msg="StopSignal SIGTERM failed to stop container 9f96"',
        want = "warn",
    },
    {
        label = "influxdb.warn.logfmt",
        line = 'ts=2099-01-02T03:04:05Z lvl=warning msg="influxdb warning"',
        want = "warn",
    },
    {
        label = "netdata.error.text",
        line = "level=error msg=\"start watching '/etc/netdata/scripts.d': no such file or directory\" plugin=scripts.d",
        want = "error",
    },
    {
        label = "nexus.warn",
        line = "2026-06-03 17:40:59,568+0000 WARN  [periodic-9-thread-7] *SYSTEM org.sonatype.nexus.selfhosted.internal.jvm.MemoryMonitor - *SYSTEM [jvm monitor] [memory] High heap",
        want = "warn",
    },
    {
        label = "nexus.info",
        line = "2026-06-03 17:41:03,973+0000 INFO  [qtp1550551852-205] *UNKNOWN org.sonatype.nexus.repository.httpclient.internal.HttpClientFacetImpl - Repository status for py",
        want = "info",
    },
    {
        label = "paperless.error",
        line = '[2026-06-03 19:41:18,019] [ERROR] [kombu.asynchronous.hub] Error in timer: ResponseError("unknown command")',
        want = "error",
    },
    { label = "sonarr.info", line = "[Info] RssSyncService: Starting RSS Sync ", want = "info" },
    {
        label = "profilarr.debug.text",
        line = "2026-06-03 19:41:48 - apscheduler.scheduler - DEBUG - Looking for jobs to run",
        want = "debug",
    },
    {
        label = "nginx.warn",
        line = "2026/06/03 17:41:02 [warn] 3953#3953: *42051 a client request body is buffered to a temporary file /var/lib/nginx/body/0000000112",
        want = "warn",
    },
    {
        label = "dnscrypt.notice.text",
        line = "[2026-06-03 05:09:34] [NOTICE] Anonymizing queries for [dct-fr] via [anon-cs-fr]",
        want = "info",
    },
    {
        label = "postgres.log",
        line = "2026-06-03 19:41:25.365 CEST [212] LOG:  checkpoint starting: time",
        want = "info",
    },
    {
        label = "temp.critical.text",
        line = "temperature sensor 'temperature_nct6798-isa-0290_temp3_AUXTIN0' transitioned from state 'alarm' to 'critical' [device 'nct6798']",
        want = "fatal",
    },
    { label = "nokw.default.text", line = "netmap: suggested exit node:  ()", want = "info" },
    {
        label = "headscale.inf",
        line = "2026-06-10T08:54:39Z INF Received signal to stop, shutting down gracefully signal=terminated",
        want = "info",
    },
    {
        label = "headscale.wrn",
        line = "2026-06-10T08:54:40Z WRN Listening without TLS but ServerURL does not start with http://",
        want = "warn",
    },
    {
        label = "headscale.err",
        line = "2026-06-14T07:26:08Z ERR user msg: node not found code=404",
        want = "error",
    },
}

for _, case in ipairs(message_cases) do
    check(case.label, sev(case.line), case.want)
end

-- Native host services fall back to journald PRIORITY when the body has no
-- level keyword in its first 120 chars ("JSONDecodeError" has no word
-- boundary before "error").
do
    local t = sev(
        "[pug] alarm_log fetch failed: JSONDecodeError: Expecting value: line 1 column 1 (char 0)",
        nil,
        { PRIORITY = "3" }
    )
    check("host-priority.error", t, "error")
end

-- Container PRIORITY remains ignored because it only describes the
-- stdout/stderr stream.
do
    local t = sev("container line without a level", nil, { PRIORITY = "3", CONTAINER_TAG = "fixture" })
    check("container-priority.ignored", t, "info")
end

-- An explicit body level wins over the native journal priority.
do
    local t = sev("WARNING host message", nil, { PRIORITY = "6" })
    check("host-priority.body-wins", t, "warn")
end

-- Native command/audit sources whose prose contains option names trust
-- their real journal priority instead of body keywords.
do
    local t = sev(
        "      ak : PWD=/home/ak ; USER=root ; COMMAND=/usr/sbin/smartctl -l error /dev/nvme0n1",
        nil,
        { PRIORITY = "5", SYSLOG_IDENTIFIER = "sudo" }
    )
    check("sudo.priority-only", t, "info")
end

do
    local t = sev(
        "Package id 0:  +46.0 C  (high = +80.0 C, crit = +100.0 C)",
        nil,
        { PRIORITY = "6", SYSLOG_IDENTIFIER = "sensors" }
    )
    check("sensors.priority-only", t, "info")
end

-- COMM comes from the sending process, SYSLOG_IDENTIFIER from the writer. A
-- record naming a trusted program it did not come from must still be scanned,
-- or any local writer could mute itself.
do
    local t = sev(
        "error: authentication failure for root",
        nil,
        { PRIORITY = "7", SYSLOG_IDENTIFIER = "sudo", COMM = "attacker" }
    )
    check("forged-identifier.body-wins", t, "error")
end

do
    local t = sev(
        "      ak : PWD=/home/ak ; USER=root ; COMMAND=/usr/sbin/smartctl -l error /dev/nvme0n1",
        nil,
        { PRIORITY = "5", SYSLOG_IDENTIFIER = "sudo", COMM = "sudo" }
    )
    check("sudo.comm-matches", t, "info")
end

-- nginx access logs are pinned directly to debug and their attacker-controlled
-- URLs are not scanned for level keywords.
do
    local t = sev(
        '10.89.0.4 - - [03/Jun/2026:17:41:02 +0000] "GET /admin/error-report?fatal=1 HTTP/1.1" 200 12',
        "nginx.access"
    )
    check("nginx.access.direct", t, "debug")
end

-- The nginx access rule is tag-scoped; other records are scanned normally.
do
    local t = sev("real ERROR happened", "svc.foo.service")
    check("nginx.access.gate-scoped", t, "error")
end

-- A canonical upstream level stays untouched.
do
    local record = {
        log = "this body says ERROR but severity is already warn",
        _level = "warn",
    }
    local code = set_priority("svc.some.service", 0, record)
    check("prepinned.code", code, 0)
    check("prepinned.kept", record["_level"], "warn")
end

-- ANSI cleanup still marks an otherwise pre-pinned record as changed.
do
    local record = { log = "\27[1;32mOK\27[0m done", _level = "info" }
    local code = set_priority("svc.some.service", 0, record)
    check("prepinned.ansi.code", code, 1)
    check("prepinned.ansi.clean", record.log, "OK done")
end

do
    local record = { log = "structured", _level = "Information" }
    local code = set_priority("svc.some.service", 0, record)
    check("structured.alias.code", code, 1)
    check("structured.alias.level", record["_level"], "info")
end

do
    local record = { log = "structured", _level = "mystery" }
    local code = set_priority("svc.some.service", 0, record)
    check("structured.unknown.code", code, 1)
    check("structured.unknown.level", record["_level"], "warn")
    check("structured.unknown.error", record.parse_error, "level")
end

-- Non-string body (already-structured record): return 0, no level set.
do
    local record = { log = 42 }
    local code = set_priority("svc.x.service", 0, record)
    check("nonstring.code", code, 0)
    check("nonstring.nolevel", record["_level"], nil)
end

-- seerr wraps its level token in SGR colour codes. Cleanup must happen before
-- classification so the token remains visible to the boundary matcher.
do
    local record = { log = "2026-06-09T20:21:00.016Z [\27[34mdebug\27[39m][Jobs]: Starting" }
    local code = set_priority("svc.seerr.service", 0, record)
    check("seerr.ansi.clean", record.log, "2026-06-09T20:21:00.016Z [debug][Jobs]: Starting")
    check("seerr.ansi.level", record["_level"], "debug")
    check("seerr.ansi.code", code, 1)
end

-- Cursor controls use the same CSI shape as colour codes.
do
    local record = { log = "\27[2K\27[1Gprogress 50%" }
    set_priority("svc.cron.service", 0, record)
    check("cursor.ansi.clean", record.log, "progress 50%")
end
