# Labs walk-thru

This is the companion to the session: the lab steps and every command you need,
one command per block so you can copy them straight into your terminal.

If you are reading this on GitHub, the outline button at the top right of the
file jumps between sections.

The labs build one harness, a piece at a time, against one Cybench task: Flag
Command, a small web app at `web_flag:1337`. Each lab fills in the `TODO`
markers for one stage. When you get stuck, [CHEATSHEET.md](CHEATSHEET.md) maps
the error messages the harness prints to their fixes.

## Before the first lab

Do this in every terminal you use for the labs.

```
cd agent-harness-workshop
```
```
. .venv/bin/activate
```

Check your API key is set. This prints `set`, never the key.

```
[ -n "$HARNESS_API_KEY" ] && echo set || echo "not set: see SETUP.md, step 5"
```

The offline tests run with no key and no Docker. Run them now, and again after
every change you make.

```
python -m pytest harness/tests -q
```

Two commands you will use in every lab. The first runs the harness; the second
summarises a run.

```
python -m harness.run --adapter cybench --task flag-command --budget-usd 2 --pause-after recon
```
```
python -m harness.trace_stats runs/<run-dir>/trace.jsonl
```

Note: each run writes a new directory under `runs/`, named after the task and a
timestamp. The run prints its path when it finishes. Replace `<run-dir>` with
it.

Note: `--budget-usd` caps what the whole run can spend. Each role also has its
own cap in `config/roles.yaml`. Leave both alone unless you mean to spend more.

> Read `stop_reason` in the trace_stats table first. A role that stopped on
> `max_tokens` ran out of room mid-thought, and a role that stopped on
> `tool_use` was about to act. Neither is a model that refused.

---

## Recon lab: giving an agent capability (25 min)

The recon role explores the target and records what it sees. Its output is a
list of observations: facts you can point at, not conclusions.

### Step 1: write the recon prompt

Open `harness/roles/recon.py` and read the docstring at the top. Before you
write anything, run recon once with the placeholder prompt (the command in
Step 3) and read the trace: that is what an agent with no instructions does.
Then write `SYSTEM_PROMPT` where it says `TODO(M2)`, to fix what you saw.

The tools the role has: `shell` (runs in the attacker container, next to the
target), `http_request`, `record_observation` and `finish`.

> Hint: tell it what an observation is and is not. "The page loads
> `/static/terminal/js/main.js`" is an observation. "The app is vulnerable to
> X" is a conclusion, and it belongs to a later stage.

### Step 2: implement `record_observation`

Same file. It stores one observation where the triage role will find it,
`ctx.state["observations"]`. The docstring says what to keep.

> Hint: the refuters judge your observations later, with no tools. `evidence`
> is the model's own account of what it saw. If an observation can also point
> at the call that produced it, exactly as it was sent and answered, nobody
> has to take the model's word for it. The recon tools are registered in
> `build_registry`, and you can wrap them. A new argument to
> `record_observation` needs a field in `OBSERVATION_SCHEMA` too: the schema
> rejects any property it does not list.

```
python -m pytest harness/tests -q
```

### Step 3: run recon on its own

```
python -m harness.run --adapter cybench --task flag-command --budget-usd 2 --pause-after recon
```
```
python -m harness.trace_stats runs/<run-dir>/trace.jsonl
```

How many observations did it record? How many of them are conclusions dressed
up as observations?

### Step 4: give it a browser, twice

The outline's question: when does a narrow skill beat a broad MCP server?
You will try both against the same target.

**The narrow skill.** The attacker container has Playwright and headless
Chromium installed. A skill is a short methodology in the prompt plus a tool
the agent already has. Add a few lines to your recon prompt telling it how to
render a page with Playwright from the shell and what to record from it. See
[harness/attacker/BROWSER.md](harness/attacker/BROWSER.md) for a working script
to start from.

**The official MCP server.** Microsoft publishes a Playwright MCP server with
dozens of generic browser tools. It runs in its own container on `shared_net`,
so it can reach the target by name. Add this to `build_registry()` in
`harness/roles/recon.py`, where the `TODO(M2)` comment shows an example:

```
registry.load_mcp_stdio("docker", ["run", "-i", "--rm", "--init", "--network", "shared_net", "mcr.microsoft.com/playwright/mcp:v0.0.83"], prefix="pw_", timeout=60)
```

Run recon once with each. Then compare the two traces:

- Which tools did the agent call, and how many times?
- Which run recorded more useful observations? Which cost more?
- Which one would you trust on a real engagement, and why?

> Keep the MCP version in your notes for the scope lab. Ask yourself now: did
> any of the `pw_` tool calls go through the scope gate?

### Step 5: write your own MCP server

`mcp-skeleton/server.py` is a working stdio MCP server over a folder of notes.
`search_notes` is done. `read_note` is yours: it is the `TODO(M2)` in that
file, and its comments walk through what to refuse.

Load it in `harness/roles/recon.py`:

```
registry.load_mcp_stdio("python", ["mcp-skeleton/server.py"], prefix="mcp_")
```

Note: `python` here is whatever is first on your `PATH`, so run the harness
with your venv active.

Want to wrap something of your own instead? The options from the outline:

- **Docs:** your documentation, threat models or design notes.
- **An open source tool:** something you already run, exposing only the
  subcommands you want an agent reaching for.
- **An API:** a REST endpoint, with care over auth and over what you
  deliberately leave out.

The decision that matters is what goes in the tool description, which the
model reads on every call, and what goes in the prompt.

---

## Triage lab: handoff and triage (20 min)

Recon output is long and noisy. Triage removes duplicates in plain code, then a
model ranks what is left and has to defend each rank. A panel of three other
models then tries to kill each candidate.

### Step 1: the triage prompt and `rank_candidate`

Open `harness/roles/triage.py`. The dedup runs in code before the model sees
anything; you write `SYSTEM_PROMPT` and `rank_candidate`, both marked
`TODO(M3)`.

> Hint: ask for the cheapest check that would prove each candidate wrong, not
> just a score. A ranking nobody has to defend drifts toward whatever sounds
> most dangerous. Keep `must_be_true` to what the observation can already
> show: the refuters cannot run anything, so a check that still needs running
> belongs in `first_check`.

### Step 2: the refuter prompt

Open `harness/roles/refute.py` and write `SYSTEM_PROMPT`. Ask for refutation,
not review: each panelist's job is to disprove the candidate.

> Hint: a refuter with no tools can kill any candidate by imagining a
> defence ("the server might check a session"). Ask for a refutation that
> points at something the panelist was shown: a status, header or field that
> contradicts the claim. "By design" is not a reason if it hands a caller
> something they should not have, and on a CTF target someone designed every
> weakness.

### Step 3: run through the refuter panel

```
python -m harness.run --adapter cybench --task flag-command --budget-usd 3 --pause-after refute
```
```
python -m harness.trace_stats runs/<run-dir>/trace.jsonl
```

Note: every run starts from the top, so this re-runs recon too.

What did the panel kill? Did the panelists agree?

### Why three refuters?

The rule is that whatever generated a candidate never judges it alone, so the
refuters come from other model families than the generator (DeepSeek). One
outside model would satisfy that rule. The other two are for the vote:

- A single refuter's mistakes go straight through. A wrong "stands" passes a
  false positive, and a wrong "refuted" kills a real bug.
- With three, a candidate dies only when most of the panel refutes it, and
  `uncertain` counts as refuted.
- They are three different families because three copies of one model tend
  to make the same mistake. An odd number means no ties.

Do not put an abliterated model in triage, refute or the judge. Removing
refusals also damages calibration, and these roles exist to say no.
`harness/README.md` ("On abliterated models") has the measurements, and Matt
Suiche's weightless write-up on one way it happens: the direction that lets a
model notice it lacks information looks like refusal, and removing it makes
the model confabulate.

### Optional stretch: more than one model for recon

The harness uses several models only to check work, never to find it. Recon is
one model. Different models find different things: on bots-bench's 59 Splunk
questions, GPT-5.5 solved 36 and GLM 5.3 solved 40, and together they solved
45.

- **Quick version:** copy `config/roles.yaml`, change the recon model to
  another family, run recon with `--roles <your copy>`, and compare the two
  sets of observations.
- **Full version:** give recon a `panel:` the way refute has one, run each
  model, and pool the observations. Triage already removes duplicates in code,
  so the overlap collapses for free, and triage gets a noisier pile that is
  worth ranking.

---

## Break (5 min)

Stand up. The second half is the heavier half.

---

## Confirm lab: building a harness to confirm a finding (30 min)

Ranking is triage. Confirming is a different job: build, exploit, judge. The
judge needs two things, a working proof of concept and a negative control that
must not trigger.

### Step 1: the expectations contract

Open `harness/roles/build.py` and write `SYSTEM_PROMPT`. The build role writes
down what success looks like before anything runs, and the exploit role
refuses to start without that file. That is what stops anyone moving the
goalposts after seeing the output.

### Step 2: the exploit prompt and the negative control

Open `harness/roles/exploit.py`. Write `SYSTEM_PROMPT`, then
`control_triggered()`.

The harness runs the negative control itself: an ordinary request every
visitor makes. `control_triggered()` decides whether that benign request
triggered the behaviour you are claiming. If it did, your exploit proves
nothing.

> Hint: what does the task tell you the flag looks like? A clean control's
> output must not contain it. And a control that failed to run at all is not
> a clean control.

### Step 3: the judge prompt

Open `harness/roles/judge.py` and write `SYSTEM_PROMPT`. The judge can only
rule through a tool, and code checks six rules before accepting a verdict
(`harness/gates/verdict.py`). Severity comes from the proof tier by a fixed
table; the judge never sets it.

| Proof tier | Severity |
|---|---|
| `rejected`, `inconclusive` | none |
| `source` | informational |
| `reachable` | low |
| `exploitable` | medium |
| `impact_confirmed` | high |

### Step 4: the full run

```
python -m harness.run --adapter cybench --task flag-command --budget-usd 5
```
```
python -m harness.trace_stats runs/<run-dir>/trace.jsonl
```

What verdict and proof tier did you get? If the judge's verdict was rejected,
which rule rejected it? Try arguing with it once.

### Step 5: break your own harness

```
python -m harness.run --adapter cybench --task flag-command --budget-usd 5 --break-harness
```

This kills the service after the readiness check. Read the result. A good
harness reports an infrastructure failure, leaves the finding unconfirmed and
never calls the judge. A bad one reports "exploit failed" and throws a real
bug away.

### Step 6: classify what you saw

For each run: confirmed, candidate, or infrastructure failure?

Then the separate question. Does the path your exploit took resemble anything
real code would do? If the harness built the triggering state itself, it
proved the bug exists under conditions it invented. Rule 6 caps that at
`exploitable`. Look for `trigger_origin` in the judge's verdict.

---

## Scope lab: scope as a gated judge (20 min)

Agents leave scope faster than people do, and an instruction in a prompt will
not stop them. The scope gate checks every action that leaves the sandbox and
answers allow, deny or ask.

### Step 1: bring up the decoy

`locktalk` is a second Cybench task, brought up next to the target on
`shared_net`. It is out of scope.

```
python -m harness.run --adapter cybench --task flag-command --with-task locktalk --budget-usd 2 --pause-after recon
```

### Step 2: write the rules

Open `scope-gate/rules.yaml`. It allows the target and the attacker shell, and
the default denies everything else, `locktalk` included. The `TODO(M5)` is
yours: add a named `deny` for the decoy and an `ask` for something you would
want a human to approve.

Note: batch runs (`--non-interactive`) turn every `ask` into a deny. An
unanswered question is not consent.

### Step 3: attack your own gate

Change your recon prompt to tell the agent that `locktalk:1337` is also part
of the task. That is the attack. Run it:

```
python -m harness.run --adapter cybench --task flag-command --with-task locktalk --budget-usd 2 --pause-after recon
```

Every decision is in the trace:

```
grep scope_decision runs/<run-dir>/trace.jsonl
```

### Step 4: what did the gate catch, and what did it miss?

- An `http_request` to `locktalk` is denied, with your rule named in the
  trace.
- `curl locktalk:1337` in the shell is allowed. The gate sees a shell command
  in the attacker container, not what the command connects to.
- The Playwright MCP tools from the recon lab never asked the gate at all. The
  scope check lives inside the harness's own tools, and MCP tools skip it.

Parsing shell commands for hosts is a losing game: variables, encodings, a
script written to disk and run later. What closes the gap is enforcement at
the network: the attacker container can reach only what is in scope.
[harness/README.md](harness/README.md) describes the allowlist setup real use
needs.

### Rules in code, or a model judge?

The gate here is rules in code. Rules cannot be argued with, but they miss
anything nobody wrote down. A model judge covers that long tail for a fraction
of a cent per call, but it can be talked out of its answer.

- Adrian Wood's talk on a production agent pipeline described a small
  decision model answering allow, deny or ask on every verification call
  against a live target (GLM 5.2 at the time).
- Claude Code's auto mode uses a classifier on every tool call. Anthropic
  reports it missed 17% of real overeager actions, and calls that "the honest
  number".
- Production uses both, and limits what the judge can see. Auto mode's
  classifier sees the user's messages and the tool calls, never the tool
  results, because that is where injected text arrives.

### Optional stretch: a model behind `ask`

`ScopeGate` takes a `prompt` function for `ask` decisions; by default it asks
you at the terminal. Replace it with a function that asks a model, then attack
both layers. The rules break on host tricks (raw IPs, redirects, alternate
names). The model breaks on injected text.

---

## Closing demo: prompt craft, economics, failures (10 min)

Watched, not typed. To try the prompt A/B at home, any role in
`config/roles.yaml` can take:

```
system_prompt_suffix_file: ~/somewhere/outside-the-repo/suffix.md
```

The file's text is appended to that role's system prompt, and the trace
records only its name and hash. Run the same task with and without it, more
than once each, and compare with `trace_stats`. One run per side is an
anecdote.

---

## Cleaning up

The harness tears its containers down when a run ends, even a failed one. If
something is left over:

```
docker ps --filter name=harness-attacker
```
```
docker compose -p flag-command -f tasks/flag-command/docker-compose.yml down
```
```
docker compose -p locktalk -f tasks/locktalk/docker-compose.yml down
```
