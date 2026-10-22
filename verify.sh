#!/usr/bin/env bash
# Pre-flight check. Run this days before the workshop, not on the morning.
set -uo pipefail
fail=0
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=1; }

echo "Agent harness workshop — pre-flight check"
echo

command -v docker >/dev/null 2>&1 && ok "docker installed" || bad "docker not found"
docker info >/dev/null 2>&1        && ok "docker daemon running" || bad "docker daemon not running"
command -v git >/dev/null 2>&1     && ok "git installed" || bad "git not found"

# TODO: benchmark images present
# TODO: API key reaches a model
# TODO: starter files intact

echo
if [ "$fail" -eq 0 ]; then
  echo "All checks passed."
else
  echo "Something above needs fixing. The room opens fifteen minutes early — bring this output."
  exit 1
fi
