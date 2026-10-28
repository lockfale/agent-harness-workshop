# Cheat sheet

The messages the harness prints, and what to do about each one. Search this
page for the first few words of your error.

## Before a run starts

**`refusing to start: HARNESS_API_KEY is not set`**

Export your key in this terminal (SETUP.md, step 5):

```
export HARNESS_API_KEY=sk-or-...
```

**`refusing to start: N model(s) are not priced: ...`**

A model in `config/roles.yaml` has no row in `config/prices.yaml`. Check the
spelling first: OpenRouter writes Anthropic versions with dots
(`claude-sonnet-5.5`). Running a local model? Give it a row at zero
(SETUP.md, step 5). The harness never guesses a price.

**`config problem: ...`**

`config/roles.yaml` is missing, or a role in it is missing a field. Compare it
with `git diff config/roles.yaml`.

**`refusing to start: run directory ... is not empty`**

A run's evidence is the files in its directory, so the harness will not reuse
one. Leave out `--run-dir`, or give it a new path.

**`note: no scope rules matched task '...'`**

The gate has no rules for this task, so it will deny everything that leaves
the sandbox. Check `scope-gate/rules.yaml` has a block under `tasks:` for the
task id the harness printed.

**`warning: Docker network 'shared_net' can reach the internet`**

You created `shared_net` before it was made internal. Recreate it:

```
docker network rm shared_net && docker network create --internal shared_net
```

**`docker is not on PATH`, or `Cannot connect to the Docker daemon`**

Start Docker Desktop (or the Docker service) and run `./verify.sh` again.

**Compose complains about `!reset`**

Your Docker Compose is older than 2.24.4. Update Docker.

## During a run

**`stop_reason` is `content_filter`, with no tokens used**

The provider refused the request before the model produced anything.
Anthropic and OpenAI models can refuse offensive security work, CTF tasks
included, unless your account has completed that provider's cyber
verification. See SETUP.md, step 1. It is not your prompt.

**`stop_reason` is `max_tokens`**

The model ran out of output room mid-thought. It did not refuse. Read what it
was doing before you change anything.

**`stop_reason` is `tool_use` on the last turn, and the role ended anyway**

The model was about to act when something else ended the run, usually a
budget cap. Look at the `budget` events in the trace.

**`... budget exceeded: $X of $Y cap (last stop_reason=...)`**

A role, or the whole run, hit its cap. The caps are in `config/roles.yaml`
and `--budget-usd`. Ask before raising either: they are spending real money.

**`the scope gate returned deny for this command. It was not run.`**

The agent tried something your scope rules do not allow. In the recon,
triage and confirm labs that is the gate working. In the scope lab it is the
point. `grep scope_decision runs/<run-dir>/trace.jsonl` shows every decision
and the rule that made it.

**`TODO(M2): implement record_observation`** (or another `TODO` in a tool
error)

The model called a tool you have not written yet. Write it, then run the
tests.

**`control_triggered() is still a TODO(M4) stub`**

The harness ran the negative control but cannot read the result until you
write `control_triggered()` in `harness/roles/exploit.py`. Until then the
judge cannot record `confirmed`.

**A provider rejects `reasoning_details`**

Set `echo_reasoning: false` on that role in `config/roles.yaml`. To stop
asking for reasoning at all, set `reasoning_effort: null`.

**`404` with `No endpoints found matching your data policy`**

Your OpenRouter privacy settings exclude every provider for that model.
Change the settings, or pick another model.

**`429` from OpenRouter**

Rate limited. Free models (IDs ending in `:free`) are capped per minute and
per day. The harness's default models are paid models, which are not.

## The recon lab's tools

**`loading MCP tools needs the optional mcp package`**

```
pip install -r harness/requirements.txt
```

**`ModuleNotFoundError: No module named 'mcp.server.fastmcp'`**

You have `mcp` 2.x, which renamed it. The labs use 1.x:

```
pip install "mcp>=1.26,<2"
```

**`MCP tool '...' timed out after 30s`**

The server took too long. A browser can be slow to start; pass `timeout=60`
to `load_mcp_stdio`.

**The Playwright MCP server cannot reach `web_flag`**

It has to run on `shared_net`. Check the `--network shared_net` argument in
your `load_mcp_stdio` call.

**`attacker image has no Playwright`** (from `./verify.sh`)

You built the attacker image before the browser was added. Rebuild it:

```
docker build -t harness-attacker:latest harness/attacker
```

## Tests

**`python -m pytest` fails right after you edited a role**

Read the failing test's name. A test that checks your code is giving you a
hint. If you think the test itself is wrong, say so in the session chat.

## Cleaning up

**Containers left over after a crash**

```
docker ps --filter name=harness-attacker
```
```
docker compose -p flag-command -f tasks/flag-command/docker-compose.yml down
```
```
docker compose -p locktalk -f tasks/locktalk/docker-compose.yml down
```
