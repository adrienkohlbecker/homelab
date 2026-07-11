#!/usr/bin/env bash
#MISE description="Run Fluent Bit Lua filter unit tests"
set -euo pipefail

# The Lua filters run inside fluent-bit's embedded interpreter on lab, which
# has no standalone CLI. The unit tests are plain Lua, so we run them under any
# system Lua. Lua isn't a fleet/mise tool (mise only offers source-compiled
# builds), so skip cleanly on developer machines where it is absent. The CI
# image includes lua5.4 and always runs this task.
lua="$(command -v lua5.4 || command -v lua5.3 || command -v lua || command -v luajit || true)"

if [[ -z "$lua" ]]; then
  echo "SKIP: no lua interpreter on PATH (brew install lua / apt install lua5.4 to run)"
  exit 0
fi

# Each shared filter has a sibling <name>_test.lua.
rc=0
for t in roles/fluentbit/files/*_test.lua; do
  echo "== $t =="
  "$lua" "$t" || rc=1
done
exit "$rc"
