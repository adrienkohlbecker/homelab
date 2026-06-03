-- Unit tests for level_from_message.lua. Run via `mise run test:fluentbit-lua`
-- (which locates a system lua); or directly: `lua5.4 level_from_message_test.lua`.
-- Exits non-zero on the first failed assertion.
--
-- The message samples are real lines captured from lab's journal
-- (journalctl -o json) across the services that actually run there, picked
-- to cover each LEVEL_RULES branch plus the deliberate quirks (notice->info,
-- LOG:->info, first-120-char scope, default-info fallback). Keep them
-- verbatim -- they are the regression corpus.

local here = arg[0]:match("^(.*/)") or "./"
dofile(here .. "level_from_message.lua")

local failures = 0

local function check(label, got, want)
    if got ~= want then
        failures = failures + 1
        print(string.format("FAIL  %s\n        got:  %s\n        want: %s", label, tostring(got), tostring(want)))
    end
end

-- Run a log line through the filter (default tag svc.test.service, no extra
-- record fields) and return (severity_text, severity_number, return_code).
local function sev(line, tag, record)
    record = record or {}
    record.log = line
    local metadata = {}
    local code = set_priority(tag or "svc.test.service", 0, nil, metadata, record)
    local otlp = metadata.otlp or {}
    return otlp.severity_text, otlp.severity_number, code
end

-- 1. podman/docker structured `level=info` (container stdout via journald).
do
    local t, n =
        sev('time="2026-06-03T17:40:42Z" level=info msg="Created exec session 8ac8367408b2 in container 768913f"')
    check("docker.info.text", t, "info")
    check("docker.info.num", n, 9)
end

-- 2. `level=warning` -- matches the "warning" keyword, maps to warn.
do
    local t, n = sev('time="2026-06-03T17:40:47Z" level=warning msg="StopSignal SIGTERM failed to stop container 9f96"')
    check("docker.warn.text", t, "warn")
    check("docker.warn.num", n, 13)
end

-- 3. netdata go.d `level=error`.
do
    local t, n =
        sev("level=error msg=\"start watching '/etc/netdata/scripts.d': no such file or directory\" plugin=scripts.d")
    check("netdata.error.text", t, "error")
    check("netdata.error.num", n, 17)
end

-- 4. nexus log4j: bare WARN token after the timestamp.
do
    local t = sev(
        "2026-06-03 17:40:59,568+0000 WARN  [periodic-9-thread-7] *SYSTEM org.sonatype.nexus.selfhosted.internal.jvm.MemoryMonitor - *SYSTEM [jvm monitor] [memory] High heap"
    )
    check("nexus.warn", t, "warn")
end

-- 5. nexus bare INFO token.
do
    local t = sev(
        "2026-06-03 17:41:03,973+0000 INFO  [qtp1550551852-205] *UNKNOWN org.sonatype.nexus.repository.httpclient.internal.HttpClientFacetImpl - Repository status for py"
    )
    check("nexus.info", t, "info")
end

-- 6. paperless/python `[ERROR]` bracket form.
do
    local t = sev(
        '[2026-06-03 19:41:18,019] [ERROR] [kombu.asynchronous.hub] Error in timer: ResponseError("unknown command")'
    )
    check("paperless.error", t, "error")
end

-- 7. *arr `[Info]` bracket form.
do
    local t = sev("[Info] RssSyncService: Starting RSS Sync ")
    check("sonarr.info", t, "info")
end

-- 8. profilarr/apscheduler `- DEBUG -` form.
do
    local t, n = sev("2026-06-03 19:41:48 - apscheduler.scheduler - DEBUG - Looking for jobs to run")
    check("profilarr.debug.text", t, "debug")
    check("profilarr.debug.num", n, 5)
end

-- 9. nginx `[warn]` form (the leading 2026/.. timestamp doesn't interfere).
do
    local t = sev(
        "2026/06/03 17:41:02 [warn] 3953#3953: *42051 a client request body is buffered to a temporary file /var/lib/nginx/body/0000000112"
    )
    check("nginx.warn", t, "warn")
end

-- 10. dnscrypt-proxy `[NOTICE]` -- deliberately maps to info, NOT warn.
do
    local t, n = sev("[2026-06-03 05:09:34] [NOTICE] Anonymizing queries for [dct-fr] via [anon-cs-fr]")
    check("dnscrypt.notice.text", t, "info")
    check("dnscrypt.notice.num", n, 9)
end

-- 11. Postgres `LOG:` chatter -- the trailing-colon rule maps to info.
do
    local t = sev("2026-06-03 19:41:25.365 CEST [212] LOG:  checkpoint starting: time")
    check("postgres.log", t, "info")
end

-- 12. A temperature "critical" -- the fatal rule's `critical` keyword;
--     crit-and-worse collapse to fatal/21.
do
    local t, n = sev(
        "temperature sensor 'temperature_nct6798-isa-0290_temp3_AUXTIN0' transitioned from state 'alarm' to 'critical' [device 'nct6798']"
    )
    check("temp.critical.text", t, "fatal")
    check("temp.critical.num", n, 21)
end

-- 13. No level keyword anywhere -> default info (so HyperDX's body-regex
--     fallback never fires).
do
    local t, n = sev("netmap: suggested exit node:  ()")
    check("nokw.default.text", t, "info")
    check("nokw.default.num", n, 9)
end

-- 14. The scan is body-text only, independent of journald PRIORITY: this
--     real line was emitted at PRIORITY=3 (err) but carries no level keyword
--     in its first 120 chars ("JSONDecodeError" has no word boundary before
--     "error"), so it defaults to info.
do
    local t = sev("[pug] alarm_log fetch failed: JSONDecodeError: Expecting value: line 1 column 1 (char 0)")
    check("body-only.default", t, "info")
end

-- 15. nginx.access pre-stamp branch: an upstream modify filter sets
--     record.severity_text=info so this filter trusts it and does NOT scan
--     the URL (which here contains "error") for keywords.
do
    local t, n = sev(
        '10.89.0.4 - - [03/Jun/2026:17:41:02 +0000] "GET /admin/error-report?fatal=1 HTTP/1.1" 200 12',
        "nginx.access",
        { severity_text = "info", severity_number = 9 }
    )
    check("nginx.access.trusted.text", t, "info")
    check("nginx.access.trusted.num", n, 9)
end

-- 16. The nginx.access trust gate is tag-scoped: the same severity_text on a
--     non-nginx.access record is ignored, and the body is scanned normally.
do
    local t = sev("real ERROR happened", "svc.foo.service", { severity_text = "info" })
    check("nginx.access.gate-scoped", t, "error")
end

-- 17. Upstream filter already pinned metadata.otlp.severity_text (flatten_csp
--     for CSP records): leave it untouched, return 0 (no modification).
do
    local record = { log = "this body says ERROR but severity is already warn" }
    local metadata = { otlp = { severity_text = "warn", severity_number = 13 } }
    local code = set_priority("csp.lab", 0, nil, metadata, record)
    check("prepinned.code", code, 0)
    check("prepinned.kept", metadata.otlp.severity_text, "warn")
end

-- 18. Non-string body (already-structured record): return 0, no severity set.
do
    local record = { log = 42 }
    local metadata = {}
    local code = set_priority("svc.x.service", 0, nil, metadata, record)
    check("nonstring.code", code, 0)
    check("nonstring.nosev", (metadata.otlp or {}).severity_text, nil)
end

if failures == 0 then
    print("level_from_message: all assertions passed")
    os.exit(0)
else
    print(string.format("level_from_message: %d assertion(s) failed", failures))
    os.exit(1)
end
