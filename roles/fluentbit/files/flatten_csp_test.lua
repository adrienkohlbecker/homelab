-- Unit tests for flatten_csp.lua. Run via `mise run test:fluentbit-lua`
-- (which locates a system lua); or directly: `lua5.4 flatten_csp_test.lua`.
-- Exits non-zero on the first failed assertion.
--
-- CSP violation reports are browser-emitted and arrive on a public,
-- unauthenticated HTTP input, so `record` is entirely attacker-controlled.
-- Rather than capture live reports (transient, PII-bearing referrers), the
-- samples are synthesised to the two wire shapes the W3C spec defines
-- (legacy report-uri object, modern Reporting API object) and to exercise the
-- security-relevant paths: URL query/fragment stripping, the scalar-only
-- pick() guard, control/bidi scrubbing, the per-field length cap, the
-- camelCase->kebab key normalize, and the attacker-key strip on return.

local here = arg[0]:match("^(.*/)") or "./"
dofile(here .. "flatten_csp.lua")

local failures = 0

local function check(label, got, want)
    if got ~= want then
        failures = failures + 1
        print(string.format("FAIL  %s\n        got:  %s\n        want: %s", label, tostring(got), tostring(want)))
    end
end

-- Run a record through the filter; return (out_record, otlp_metadata, code).
local function csp(record)
    local metadata = {}
    local code, _, md, out = flatten_csp("csp.lab", 0, nil, metadata, record)
    return out, md.otlp, code
end

-- 1. Legacy report-uri object: full field set, including URLs with query +
--    fragment (must be stripped) and a numeric line/column/status (stringified).
do
    local out, otlp = csp({
        ["csp-report"] = {
            ["blocked-uri"] = "https://evil.example/x.js?token=secret#frag",
            ["document-uri"] = "https://app.fahm.fr/page?session=abc",
            ["violated-directive"] = "script-src 'self'",
            ["effective-directive"] = "script-src",
            ["original-policy"] = "default-src 'self'",
            ["disposition"] = "enforce",
            ["source-file"] = "https://app.fahm.fr/app.js?v=1",
            ["referrer"] = "https://app.fahm.fr/?ref=x",
            ["script-sample"] = "alert(1)",
            ["line-number"] = 42,
            ["column-number"] = 7,
            ["status-code"] = 200,
        },
    })
    local a = otlp.attributes
    check("legacy.severity_text", otlp.severity_text, "warn")
    check("legacy.severity_number", otlp.severity_number, 13)
    check("legacy.format", a.csp_format, "legacy")
    check("legacy.blocked_stripped", a.csp_blocked_uri, "https://evil.example/x.js")
    check("legacy.document_stripped", a.csp_document_uri, "https://app.fahm.fr/page")
    check("legacy.violated", a.csp_violated_directive, "script-src 'self'")
    check("legacy.effective", a.csp_effective_directive, "script-src")
    check("legacy.policy", a.csp_original_policy, "default-src 'self'")
    check("legacy.disposition", a.csp_disposition, "enforce")
    check("legacy.source_file_stripped", a.csp_source_file, "https://app.fahm.fr/app.js")
    check("legacy.referrer_stripped", a.csp_referrer, "https://app.fahm.fr/")
    check("legacy.sample", a.csp_sample, "alert(1)")
    check("legacy.line_number_str", a.csp_line_number, "42")
    check("legacy.column_number_str", a.csp_column_number, "7")
    check("legacy.status_code_str", a.csp_status_code, "200")
    check("legacy.body", out.log, "CSP script-src blocked https://evil.example/x.js on https://app.fahm.fr/page")
    -- The attacker-controlled top-level key must be discarded on return.
    check("legacy.attacker_key_stripped", out["csp-report"], nil)
end

-- 2. Modern Reporting API object: camelCase keys normalize to kebab-case;
--    -url variants resolve alongside the legacy -uri picks; statusCode=0 is a
--    real value (number, stringified) not an absent field.
do
    local out, otlp = csp({
        type = "csp-violation",
        body = {
            blockedURL = "https://evil/y?a=1",
            documentURL = "https://app/z?b=2",
            violatedDirective = "img-src",
            effectiveDirective = "img-src",
            disposition = "report",
            statusCode = 0,
        },
    })
    local a = otlp.attributes
    check("reporting.format", a.csp_format, "reporting-api")
    check("reporting.blocked", a.csp_blocked_uri, "https://evil/y")
    check("reporting.document", a.csp_document_uri, "https://app/z")
    check("reporting.violated", a.csp_violated_directive, "img-src")
    check("reporting.effective", a.csp_effective_directive, "img-src")
    check("reporting.disposition", a.csp_disposition, "report")
    check("reporting.status_zero", a.csp_status_code, "0")
    check("reporting.body", out.log, "CSP img-src blocked https://evil/y on https://app/z")
end

-- 3. Unrecognised shape -> tagged body, csp_format=unknown, still warn.
do
    local out, otlp = csp({ foo = "bar", nested = { x = 1 } })
    check("unknown.body", out.log, "csplogger received malformed POST")
    check("unknown.format", otlp.attributes.csp_format, "unknown")
    check("unknown.severity", otlp.severity_text, "warn")
    check("unknown.attacker_key_stripped", out.foo, nil)
end

-- 4. Scalar-only pick() guard: a nested object at a known key (malformed POST)
--    is rejected rather than tostring()'d into a "table: 0x..." heap-pointer
--    leak. The field is simply absent.
do
    local out, otlp = csp({
        ["csp-report"] = {
            ["blocked-uri"] = { x = 1 },
            ["document-uri"] = "https://app/ok",
        },
    })
    check("nested.blocked_absent", otlp.attributes.csp_blocked_uri, nil)
    check("nested.document_ok", otlp.attributes.csp_document_uri, "https://app/ok")
    check("nested.body", out.log, "CSP ? blocked ? on https://app/ok")
end

-- 5. Scrub + length cap: bidi-override (U+202E) and a C0 control byte are
--    stripped; an over-long attacker field is capped at 1024 bytes.
do
    local payload = "https://e/\xE2\x80\xAE\x07a" .. string.rep("b", 2000)
    local _, otlp = csp({
        ["csp-report"] = {
            ["blocked-uri"] = payload,
            ["document-uri"] = "https://app/ok",
        },
    })
    local b = otlp.attributes.csp_blocked_uri
    check("scrub.no_bidi", b:find("\xE2\x80\xAE", 1, true), nil)
    check("scrub.no_control", b:find("\x07", 1, true), nil)
    check("scrub.capped_1024", #b, 1024)
    check("scrub.prefix_kept", b:sub(1, 11), "https://e/a")
end

if failures == 0 then
    print("flatten_csp: all assertions passed")
    os.exit(0)
else
    print(string.format("flatten_csp: %d assertion(s) failed", failures))
    os.exit(1)
end
