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

## 3. Docker

Required, with the benchmark images pulled **in advance**. They are not small,
and pulling them live would cost the first ten minutes of the session.

```bash
# pull commands to be added
```

## 4. This repository

```bash
git clone https://github.com/lockfale/agent-harness-workshop
cd agent-harness-workshop
./verify.sh
```

## If verify.sh fails

The room opens fifteen minutes early. Bring the output.
