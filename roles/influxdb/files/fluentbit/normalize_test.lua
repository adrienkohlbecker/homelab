-- Unit tests for InfluxDB's post-logfmt Fluent Bit normalizer. Input records
-- model the parser filter's Reserve_Data and Preserve_Key output.

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
	local code = normalize_influxdb(tag or "svc.influxdb.service", 0, record)
	return record, code
end

do
	local rec, code = normalize({
		log = 'ts=2026-07-11T02:01:21.144938Z lvl=warn msg="nats-port argument is deprecated and unused" log_id=13~jN9z0000\n',
		CONTAINER_TAG = "influxdb",
		CONTAINER_ID = "123456789abc",
		CONTAINER_ID_FULL = "123456789abcdef0",
		CMDLINE = "/usr/bin/conmon --log-tag influxdb",
		PRIORITY = "6",
		SYSTEMD_UNIT = "influxdb.service",
		SYSLOG_IDENTIFIER = "influxdb",
		ts = "2026-07-11T02:01:21.144938Z",
		lvl = "warn",
		msg = "nats-port argument is deprecated and unused",
		log_id = "13~jN9z0000",
	})
	check("warning.code", code, 1)
	check("warning.message", rec.log, "nats-port argument is deprecated and unused")
	check("warning.level", rec._level, "warn")
	check("warning.log_id", rec.log_id, "13~jN9z0000")
	check("warning.status", rec.parser_status, "parsed")
	check("warning.timestamp_removed", rec.ts, nil)
	check("warning.source_level_removed", rec.lvl, nil)
	check("warning.source_message_removed", rec.msg, nil)
	check("warning.short_container_id_kept", rec.CONTAINER_ID, "123456789abc")
	check("warning.container_tag_removed", rec.CONTAINER_TAG, nil)
	check("warning.full_container_id_removed", rec.CONTAINER_ID_FULL, nil)
	check("warning.cmdline_removed", rec.CMDLINE, nil)
	check("warning.priority_removed", rec.PRIORITY, nil)
	check("warning.unit_removed", rec.SYSTEMD_UNIT, nil)
	check("warning.identifier_removed", rec.SYSLOG_IDENTIFIER, nil)
end

do
	local rec = normalize({
		log = "raw logfmt",
		CONTAINER_TAG = "influxdb",
		ts = "2026-07-11T02:01:22Z",
		lvl = "info",
		msg = "Resources opened",
		log_id = "13~jN9z0000",
		service = "sqlite",
		path = "/var/lib/influxdb2/influxd.sqlite",
	})
	check("info.level", rec._level, "info")
	check("info.message", rec.log, "Resources opened")
	check("info.service", rec.service, "sqlite")
	check("info.path", rec.path, "/var/lib/influxdb2/influxd.sqlite")
end

do
	local rec = normalize({
		log = "raw logfmt",
		CONTAINER_TAG = "influxdb",
		ts = "2026-07-11T02:01:23Z",
		lvl = "error",
		msg = "Failed to open shard",
		log_id = "13~jN9z0000",
		error = "permission denied",
		shard = "42",
	})
	check("error.level", rec._level, "error")
	check("error.message", rec.log, "Failed to open shard")
	check("error.detail", rec.error, "permission denied")
	check("error.shard", rec.shard, "42")
end

do
	local rec = normalize({ log = "future unstructured output\n", CONTAINER_TAG = "influxdb" })
	check("malformed.status", rec.parser_status, "failed")
	check("malformed.error", rec.parse_error, "influxdb_logfmt")
	check("malformed.level", rec._level, "warn")
	check("malformed.raw", rec.log, "future unstructured output\n")
end

do
	local rec = normalize({
		log = "raw logfmt",
		CONTAINER_TAG = "influxdb",
		ts = "2026-07-11T02:01:24Z",
		lvl = "notice",
		msg = "Future level",
	})
	check("level.status", rec.parser_status, "failed")
	check("level.error", rec.parse_error, "influxdb_level")
	check("level.raw", rec.log, "raw logfmt")
end

do
	local rec, code = normalize({ log = "podman command output" })
	check("unit_output.code", code, 0)
	check("unit_output.status", rec.parser_status, nil)
	check("unit_output.raw", rec.log, "podman command output")
end

do
	local rec, code = normalize({ log = "untouched", CONTAINER_TAG = "influxdb" }, "svc.other.service")
	check("tag.code", code, 0)
	check("tag.container_tag", rec.CONTAINER_TAG, "influxdb")
end

if failures > 0 then
	os.exit(1)
end
print("PASS  influxdb normalize")
