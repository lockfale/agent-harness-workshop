# Setup

Run through this a few days before the workshop, not on the day. Finish by
running `./verify.sh`, which checks everything below and exits loudly on
anything missing.

## 1. An API key with access to multiple models — required

The harness calls models programmatically and routes different roles to
different models. That is the point of the exercise: the model that generates a
finding must never be the one that judges it.

**A subscription to a single vendor will not work.** It comes with no API
credit and only reaches that vendor's own models, so a custom harness has
nothing to point at.

What does work:

- **OpenRouter** — free account, funded with **$20**. Reaches every major model
  lineage from one endpoint. The harness's default models are paid models, so
  an unfunded account cannot run it.
- **Local Ollama** — two or three models pulled, if you would rather run
  everything on your own hardware.

OpenRouter's free models (IDs ending in `:free`, and the `openrouter/free`
router) will not carry you through the labs. On an unfunded account they are
capped at 50 requests a day, and one full harness run can take more than that.
The router also picks a different model at random for each request, which
breaks the rule that the model that generates a finding never judges it.

## 2. A coding assistant — optional but recommended

Claude Code, Codex, Cursor, or whatever you already use, to help write harness
and MCP code during the labs. A Pro or Max style subscription is fine here,
since this is interactive coding rather than programmatic model access.

## 3. This repository

```bash
git clone https://github.com/lockfale/agent-harness-workshop
cd agent-harness-workshop
```

Everything the labs need is in this clone, including the two Cybench tasks
under `tasks/`. Run `git pull` the day before; small fixes will land up to then.

## 4. Python 3.11 or newer

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r harness/requirements.txt
```

## 5. Your API key, where the harness can see it

```bash
export HARNESS_API_KEY=sk-or-...    # your OpenRouter key
```

Put that line in your shell profile, or run it in each terminal you use for the
labs. Never put the key in a file inside this repository.

Running models locally instead? Point the harness at your endpoint and give the
key any value:

```bash
export HARNESS_BASE_URL=http://localhost:11434/v1
export HARNESS_API_KEY=local
```

Two more changes for local models. The harness sends each model name to your
endpoint exactly as `config/roles.yaml` spells it, so put the names
`ollama list` shows in `roles.yaml`. The harness also refuses to start with a
model that has no price, so give each one a row in `config/prices.yaml` at
zero:

```yaml
models:
  qwen3-coder:
    input: 0
    output: 0
```

A `local/` prefix is priced at zero without a row, but it only works behind a
gateway that strips the prefix before forwarding. Ollama on its own will not
find `local/qwen3-coder`.

## 6. Docker, with the images built in advance

Docker with Compose v2.24.4 or newer. Build the images now: about 1 GB in total,
and building them live would cost the first ten minutes of the session.

```bash
docker network inspect shared_net >/dev/null 2>&1 || docker network create --internal shared_net
docker build -t harness-attacker:latest harness/attacker
docker compose -p flag-command -f tasks/flag-command/docker-compose.yml build
docker compose -p locktalk -f tasks/locktalk/docker-compose.yml build
```

`shared_net` is internal: the containers on it reach each other by name but
not the internet, so the agent cannot look up a writeup of the task. If you
created `shared_net` before October 28, it can still reach the internet and
`./verify.sh` will say so. Recreate it:

```bash
docker network rm shared_net && docker network create --internal shared_net
```

The first image is the container your agent works from. The other two are the
targets.

## 7. Check it

```bash
./verify.sh
```

## If verify.sh fails

The room opens fifteen minutes early. Bring the output.
