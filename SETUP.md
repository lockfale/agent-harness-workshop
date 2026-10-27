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
  lineage from one endpoint. Funding also raises the daily request cap from 50
  to 1,000, which matters because a single agent loop can exhaust 50 requests.
- **Local Ollama** — two or three models pulled, if you would rather run
  everything on your own hardware.

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

## 6. Docker, with the images built in advance

Docker with Compose v2.24 or newer. Build the images now: about 1 GB in total,
and building them live would cost the first ten minutes of the session.

```bash
docker network inspect shared_net >/dev/null 2>&1 || docker network create shared_net
docker build -t harness-attacker:latest harness/attacker
docker compose -p flag-command -f tasks/flag-command/docker-compose.yml build
docker compose -p locktalk -f tasks/locktalk/docker-compose.yml build
```

The first image is the container your agent works from. The other two are the
targets.

## 7. Check it

```bash
./verify.sh
```

## If verify.sh fails

The room opens fifteen minutes early. Bring the output.
