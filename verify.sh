#!/usr/bin/env bash
# Pre-flight check. Run this days before the workshop, not on the morning.
set -uo pipefail
fail=0
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=1; }

cd "$(dirname "$0")" || exit 1

echo "Agent harness workshop — pre-flight check"
echo

echo "Tools"
command -v docker >/dev/null 2>&1 && ok "docker installed" || bad "docker not found"
docker info >/dev/null 2>&1        && ok "docker daemon running" || bad "docker daemon not running"
command -v git >/dev/null 2>&1     && ok "git installed" || bad "git not found"

# The harness removes the tasks' host port mappings with `!reset`. Docker
# documents 2.24.4 for its sibling `!override` and no version for `!reset`, so
# 2.24.4 is the floor.
compose=$(docker compose version --short 2>/dev/null | sed 's/^v//')
if [ -z "$compose" ]; then
  bad "docker compose v2 not found"
else
  major=${compose%%.*}; rest=${compose#*.}; minor=${rest%%.*}
  case "$rest" in *.*) patch=${rest#*.} ;; *) patch=0 ;; esac
  patch=${patch%%[!0-9]*}; patch=${patch:-0}
  if [ "$major" -gt 2 ] || { [ "$major" -eq 2 ] && { [ "$minor" -gt 24 ] ||
       { [ "$minor" -eq 24 ] && [ "$patch" -ge 4 ]; }; }; }; then
    ok "docker compose $compose"
  else
    bad "docker compose $compose is older than 2.24.4"
  fi
fi

py=python3
[ -x .venv/bin/python ] && py=.venv/bin/python
if "$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
  ok "python $("$py" -c 'import platform; print(platform.python_version())') ($py)"
else
  bad "python 3.11 or newer not found (tried $py)"
fi
if "$py" -c 'import openai, jsonschema, yaml' 2>/dev/null; then
  ok "harness dependencies installed"
else
  bad "harness dependencies missing: pip install -r harness/requirements.txt"
fi

echo
echo "Repository"
missing=0
for f in harness/requirements.txt harness/attacker/Dockerfile config/roles.yaml config/prices.yaml \
         tasks/flag-command/docker-compose.yml tasks/locktalk/docker-compose.yml; do
  [ -f "$f" ] || { bad "missing $f (git pull, or re-clone)"; missing=1; }
done
[ "$missing" -eq 0 ] && ok "starter files present"

echo
echo "Docker images"
# shared_net must be internal: the containers reach each other by name but not
# the internet, so the agent cannot look up a writeup of the task.
if ! docker network inspect shared_net >/dev/null 2>&1; then
  bad "network shared_net missing: docker network create --internal shared_net"
elif [ "$(docker network inspect -f '{{.Internal}}' shared_net 2>/dev/null)" = "true" ]; then
  ok "network shared_net (internal, no internet)"
else
  bad "network shared_net can reach the internet: docker network rm shared_net && docker network create --internal shared_net"
fi
for img in harness-attacker:latest flag-command-web_flag:latest locktalk-locktalk:latest; do
  docker image inspect "$img" >/dev/null 2>&1 \
    && ok "image $img" \
    || bad "image $img not built (see SETUP.md, step 6)"
done

echo
echo "Model access"
base=${HARNESS_BASE_URL:-https://openrouter.ai/api/v1}
base=${base%/}
if [ -z "${HARNESS_API_KEY:-}" ]; then
  bad "HARNESS_API_KEY is not set (see SETUP.md, step 5)"
else
  # OpenRouter's /key endpoint checks the key without spending anything. For
  # any other endpoint, listing models is the cheapest authenticated call.
  case "$base" in
    *openrouter.ai*) url="$base/key" ;;
    *)               url="$base/models" ;;
  esac
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
           -H "Authorization: Bearer $HARNESS_API_KEY" "$url")
  case "$code" in
    200) ok "API key accepted by ${base#*://}" ;;
    401|403) bad "API key rejected by ${base#*://} (HTTP $code)" ;;
    000) bad "could not reach ${base#*://}" ;;
    *)   bad "unexpected HTTP $code from $url" ;;
  esac
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "All checks passed."
else
  echo "Something above needs fixing. The room opens fifteen minutes early — bring this output."
  exit 1
fi
