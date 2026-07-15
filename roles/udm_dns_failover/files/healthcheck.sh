#!/bin/bash
set -euo pipefail
# Probe dnsmasq on the UDM's own loopback, not the VIP. dnsmasq only
# binds the VIP after keepalived assigns it, so probing VIP:53 deadlocks
# (healthcheck fails → FAULT → VIP never assigned → dnsmasq never binds).
#
# dig (not a bare TCP connect) exercises real UDP resolution: any reply --
# even NXDOMAIN/SERVFAIL -- proves dnsmasq is alive and answering, while no
# reply (daemon down) exits non-zero. The query name is irrelevant (we only
# care that it responds) and stays local so the probe doesn't depend on
# upstream. dig ships in UniFi OS, surviving firmware updates like dnsmasq.
exec dig +tries=1 +time=2 +short @127.0.0.1 localhost > /dev/null
