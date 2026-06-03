#!/usr/bin/env bash
#MISE description="Run the fluent-bit Lua filter unit tests (parser, severity, otlp-shape, csp)"
set -euo pipefail

# The Lua filters run inside fluent-bit's embedded interpreter on lab, which
# has no standalone CLI. The unit tests are plain Lua, so we run them under any
# system Lua. Lua isn't a fleet/mise tool (mise only offers source-compiled
# builds), so locate whatever the developer has; skip cleanly where absent
# (e.g. the CI container) rather than failing a check we can't run there.
lua=""
for cand in lua5.4 lua5.3 lua luajit; do
  if command -v "$cand" >/dev/null 2>&1; then
    lua="$cand"
    break
  fi
done

if [[ -z "$lua" ]]; then
  echo "SKIP: no lua interpreter on PATH (brew install lua / apt install lua5.4 to run)"
  exit 0
fi

# Each filter has a sibling <name>_test.lua; run them all, fail if any fails.
rc=0
for t in roles/fluentbit/files/*_test.lua; do
  echo "== $t =="
  "$lua" "$t" || rc=1
done
exit "$rc"
