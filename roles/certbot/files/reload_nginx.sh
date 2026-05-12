#!/bin/bash
# certbot deploy-hook: reload nginx so it picks up the renewed cert.
# Runs after every successful `certbot renew` from certbot.timer.
# No-op when nginx isn't active (host doesn't run nginx, or it's already
# stopped) so the renewal itself never fails on this hook.
set -euo pipefail

if systemctl is-active --quiet nginx; then
  systemctl reload nginx
fi
