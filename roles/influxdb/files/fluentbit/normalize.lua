-- Normalize InfluxDB's native logfmt container output after the built-in
-- parser has merged application fields into the journald record.

local LEVELS = {
	debug = "debug",
	info = "info",
	warn = "warn",
	warning = "warn",
	error = "error",
	fatal = "fatal",
}

local DISCARD = {
	"ts",
	"lvl",
	"msg",
	-- Journald/conmon fields duplicated by the final shaper or its tag-derived
	-- service/unit fields. Keep only the short container id and application
	-- metadata such as log_id, service, path, and error.
	"BOOT_ID",
	"CAP_EFFECTIVE",
	"CMDLINE",
	"CODE_FILE",
	"CODE_FUNC",
	"CODE_LINE",
	"COMM",
	"CONTAINER_ID_FULL",
	"CONTAINER_NAME",
	"CONTAINER_TAG",
	"EXE",
	"GID",
	"HOSTNAME",
	"MACHINE_ID",
	"PID",
	"PRIORITY",
	"SELINUX_CONTEXT",
	"SOURCE_REALTIME_TIMESTAMP",
	"SYSLOG_IDENTIFIER",
	"SYSTEMD_CGROUP",
	"SYSTEMD_INVOCATION_ID",
	"SYSTEMD_SLICE",
	"SYSTEMD_UNIT",
	"TRANSPORT",
	"UID",
}

local function mark_failure(record, reason)
	record["parser_status"] = "failed"
	record["parse_error"] = reason
	record["_level"] = "warn"
end

function normalize_influxdb(tag, ts, record)
	if tag ~= "svc.influxdb.service" or record["CONTAINER_TAG"] == nil then
		return 0, ts, record
	end

	local raw = record["log"]
	if type(raw) ~= "string" then
		mark_failure(record, "influxdb_message")
		return 1, ts, record
	end

	if type(record["ts"]) ~= "string" or type(record["lvl"]) ~= "string" or type(record["msg"]) ~= "string" then
		mark_failure(record, "influxdb_logfmt")
		return 1, ts, record
	end

	local level = LEVELS[string.lower(record["lvl"])]
	if level == nil then
		mark_failure(record, "influxdb_level")
		return 1, ts, record
	end

	record["log"] = record["msg"]:gsub("[\r\n]+$", "")
	record["_level"] = level
	record["parser_status"] = "parsed"

	for _, key in ipairs(DISCARD) do
		record[key] = nil
	end

	return 1, ts, record
end
