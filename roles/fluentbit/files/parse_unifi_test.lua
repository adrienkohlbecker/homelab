-- Unit tests for parse_unifi.lua. Run via `mise run test:fluentbit-parser`
-- (which locates a system lua); or directly: `lua5.4 parse_unifi_test.lua`.
-- Exits non-zero on the first failed assertion.
--
-- The samples are real payloads captured off the wire on lab (tcpdump -A
-- udp port 5140) covering the three UniFi formats plus the no-delimiter
-- fallback. Keep them verbatim -- they are the regression corpus.

local here = arg[0]:match("^(.*/)") or "./"
dofile(here .. "parse_unifi.lua")

local failures = 0

-- Run a raw line through the filter and return (record, severity_text).
local function run(line)
    local record = { log = line }
    local metadata = {}
    parse_unifi("unifi", 0, nil, metadata, record)
    return record, (metadata.otlp or {}).severity_text
end

local function check(label, got, want)
    if got ~= want then
        failures = failures + 1
        print(string.format("FAIL  %s\n        got:  %s\n        want: %s",
            label, tostring(got), tostring(want)))
    end
end

-- 1. Gateway daemon syslog: doubled hostname, "kernel" program, PRI 4 -> warn.
do
    local r, sev = run("<4>Jun  3 16:32:52 DreamMachinePro DreamMachinePro kernel: [quic_sm_reassemble_func#1025]: failed to allocate reassemble cont.")
    check("daemon.log", r.log, "[quic_sm_reassemble_func#1025]: failed to allocate reassemble cont.")
    check("daemon.program", r.unifi_program, "kernel")
    check("daemon.source", r.unifi_source, "DreamMachinePro")
    check("daemon.severity", sev, "warn")
end

-- 2. Daemon syslog where the message itself contains ": " (systemd unit line).
do
    local r = run("<28>Jun  3 16:32:54 DreamMachinePro DreamMachinePro systemd[1]: udapi-server.service: Got notification message from PID 470652, but reception only permitted for main PID 2193")
    check("systemd.log", r.log, "udapi-server.service: Got notification message from PID 470652, but reception only permitted for main PID 2193")
    check("systemd.program", r.unifi_program, "systemd[1]")
end

-- 3. AP device syslog: "mac,model-version" program tag is split out.
do
    local r, sev = run("<30>Jun  3 16:33:07 AccessPointnanoHD e063daccbd48,UAP-nanoHD-6.7.41+15623: mcad: mcad[21498]: wireless_agg_stats.log_sta_anomalies(): bssid=e2:63:da:ac:bd:4a radio=rai0 vap=rai2 sta=c2:5b:04:93:bb:fe satisfaction_now=70 anomalies=low_phy_rate")
    check("ap.mac", r.unifi_device_mac, "e063daccbd48")
    check("ap.model", r.unifi_device_model, "UAP-nanoHD-6.7.41+15623")
    check("ap.program_absent", r.unifi_program, nil)
    check("ap.source", r.unifi_source, "AccessPointnanoHD")
    check("ap.log", r.log, "mcad: mcad[21498]: wireless_agg_stats.log_sta_anomalies(): bssid=e2:63:da:ac:bd:4a radio=rai0 vap=rai2 sta=c2:5b:04:93:bb:fe satisfaction_now=70 anomalies=low_phy_rate")
    check("ap.severity", sev, "info")  -- PRI 30 % 8 = 6 = info
end

-- 4. CEF event: header fields + variable extension with space-containing
--    values; the `msg` extension becomes the body, UNIFI* prefix stripped.
do
    local r, sev = run("<14>Jun  3 16:30:31 DreamMachinePro CEF:0|Ubiquiti|UniFi Network|10.4.57|400|WiFi Client Connected|1|UNIFIcategory=Client Devices UNIFIhost=Dream Machine Pro UNIFIconnectedToDeviceName=UAP-IW-HD UNIFIconnectedToDeviceIp=10.123.0.11 UNIFIconnectedToDeviceMac=e0:63:da:22:80:d6 UNIFIconnectedToDeviceModel=UAP-IW-HD UNIFIconnectedToDeviceVersion=6.7.41 UNIFIclientAlias=esphome-somfy 4c:18 UNIFIclientHostname=esphome-somfy UNIFIclientIp=10.123.4.13 UNIFIclientMac=d4:8a:fc:c6:4c:18 UNIFIwifiChannel=11 UNIFIwifiChannelWidth=20 UNIFIwifiName=Canards IoT UNIFIwifiBand=ng UNIFIauthMethod=wpapsk UNIFIWiFiRssi=-73 UNIFInetworkName=IoT UNIFInetworkSubnet=10.123.4.0/24 UNIFInetworkVlan=4 UNIFIutcTime=2026-06-03T14:30:31.271Z msg=esphome-somfy 4c:18 connected to Canards IoT on UAP-IW-HD. Connection Info: Ch. 11 (2.4 GHz, 20 MHz), -73 dBm. IP: 10.123.4.13")
    check("cef.vendor", r.unifi_vendor, "Ubiquiti")
    check("cef.product", r.unifi_product, "UniFi Network")
    check("cef.device_version", r.unifi_cef_device_version, "10.4.57")
    check("cef.signature_id", r.unifi_cef_signature_id, "400")
    check("cef.name", r.unifi_cef_name, "WiFi Client Connected")
    check("cef.severity_field", r.unifi_cef_severity, "1")
    -- Extension values that contain spaces must survive intact.
    check("cef.category", r.unifi_category, "Client Devices")
    check("cef.host", r.unifi_host, "Dream Machine Pro")
    check("cef.wifiName", r.unifi_wifiName, "Canards IoT")
    check("cef.clientAlias", r.unifi_clientAlias, "esphome-somfy 4c:18")
    check("cef.networkVlan", r.unifi_networkVlan, "4")
    check("cef.utcTime", r.unifi_utcTime, "2026-06-03T14:30:31.271Z")
    -- The trailing msg (with colons and parens) is the body, captured whole.
    check("cef.log", r.log, "esphome-somfy 4c:18 connected to Canards IoT on UAP-IW-HD. Connection Info: Ch. 11 (2.4 GHz, 20 MHz), -73 dBm. IP: 10.123.4.13")
    check("cef.source", r.unifi_source, "DreamMachinePro")
    check("cef.severity", sev, "info")  -- PRI 14 % 8 = 6 = info
    -- The raw "msg" key must not leak as an attribute (it became the body).
    check("cef.no_raw_msg", r.msg, nil)
end

-- 5. Device line with no "<program>: " delimiter: body is the remainder.
do
    local r = run("<31>Jun  3 10:00:00 USW-Pro a1b2c3d4e5f6,USW-Pro-24-PoE-7.0.50: hostapd: nonsense without colon delimiter elsewhere")
    check("nodelim.mac", r.unifi_device_mac, "a1b2c3d4e5f6")
    check("nodelim.log", r.log, "hostapd: nonsense without colon delimiter elsewhere")
end

-- 6. Non-string body (e.g. already-structured record) passes through untouched.
do
    local record = { log = 42 }
    local metadata = {}
    local code = parse_unifi("unifi", 0, nil, metadata, record)
    check("nonstring.code", code, 0)
    check("nonstring.untouched", record.log, 42)
end

-- 7. Unrecognised envelope (no syslog timestamp): line shipped as-is.
do
    local r = run("not a syslog line at all")
    check("garbage.log", r.log, "not a syslog line at all")
    check("garbage.no_source", r.unifi_source, nil)
end

if failures == 0 then
    print("parse_unifi: all assertions passed")
    os.exit(0)
else
    print(string.format("parse_unifi: %d assertion(s) failed", failures))
    os.exit(1)
end
