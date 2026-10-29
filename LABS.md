# Labs walk-thru

This is the companion to the session: the lab steps and every command you need,
one command per block so you can copy them straight into your terminal.

If you are reading this on GitHub, the outline button at the top right of the
file jumps between sections.

The labs build one harness, a piece at a time, against one Cybench task: Flag
Command, a small web app at `web_flag:1337`. The agent is briefed as it would
be for an assessment: the harness writes its own task prompt rather than using
Cybench's capture-the-flag wording. Each lab fills in the `TODO` markers for
one stage. When you get stuck, [CHEATSHEET.md](CHEATSHEET.md) maps
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

Using a coding assistant? Start it in this folder (for Claude Code, `claude`). [AGENTS.md](AGENTS.md) tells it to give hints rather than answers, so to get code, ask for the full answer: each step below ends with a prompt that does. Run the harness yourself, not through the assistant. It has no terminal, so a pause ends the run and every scope `ask` becomes a deny.

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

> Hint: finding an exposed key is recon. Using it is the exploit stage's job.
> Tell recon to record a secret it finds and never use it.

Prompt for your coding assistant:

```text
Read the TODO(M2) comments above SYSTEM_PROMPT in harness/roles/recon.py and the hints in LABS.md recon Step 1. Then write the complete SYSTEM_PROMPT for the recon role. I'm asking for the full answer. Recon maps and records, it does not exploit: record facts with evidence as observations, record any exposed secret but never use it, call finish when the target is mapped, about 15 turns. Make only these changes: no other edits, no refactors, don't run the harness, and reply in two lines.
```

### Step 2: implement `record_observation`

Same file. It stores one observation where the triage role will find it,
`ctx.state["observations"]`. The docstring says what to keep.

`ctx.state` is the run's shared notebook: one Python dict that every stage of
the run reads and writes. Recon appends to `ctx.state["observations"]`, and
when triage starts it reads that same list. Recon's conversation is thrown
away, so this list is all triage ever sees.

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

Prompt for your coding assistant:

```text
Write the complete record_observation in harness/roles/recon.py, following its docstring. I'm asking for the full answer. Also wrap the shell and http_request tools in build_registry so every call is logged with a call_id, let record_observation take a call_id and attach that logged call, and add call_id to OBSERVATION_SCHEMA. Then add a line to the recon SYSTEM_PROMPT telling it to pass the call_id of the result that shows each fact. Fix any imports you need. Then run python -m pytest harness/tests -q. Make only these changes: no other edits, no refactors, don't run the harness, and reply in two lines.
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

**Trim it.** Those 25 tools include `browser_click`, `browser_fill_form`,
`browser_file_upload` and `browser_run_code_unsafe`: tools that change things
or run code, which is not recon's job. `only=` registers just the tools you
name, using the server's names without the prefix:

```
registry.load_mcp_stdio("docker", ["run", "-i", "--rm", "--init", "--network", "shared_net", "mcr.microsoft.com/playwright/mcp:v0.0.83"], prefix="pw_", timeout=60, only=["browser_navigate", "browser_snapshot", "browser_network_requests", "browser_console_messages", "browser_take_screenshot"])
```

Run recon once with each. Then compare the traces:

- Which tools did the agent call, and how many times?
- Which run recorded more useful observations? Which cost more?
- Which one would you trust on a real engagement, and why?

> Keep the MCP version in your notes for the scope lab. Ask yourself now: did
> any of the `pw_` tool calls go through the scope gate?

Prompts for your coding assistant, one per run:

```text
Add a short paragraph to the end of the recon SYSTEM_PROMPT telling it how to render a page with Playwright from the shell, using harness/attacker/BROWSER.md, and what to record from the rendered page. Make only these changes: no other edits, no refactors, don't run the harness, and reply in two lines.
```

```text
Remove the Playwright paragraph you just added to the recon prompt. Then add the Playwright MCP line from LABS.md recon Step 4 to build_registry in harness/roles/recon.py, just before register_finish. Make only these changes: no other edits, no refactors, don't run the harness, and reply in two lines.
```

```text
Change that Playwright MCP line to the trimmed version from LABS.md recon Step 4, with only= set to browser_navigate, browser_snapshot, browser_network_requests, browser_console_messages and browser_take_screenshot. Make only these changes: no other edits, no refactors, don't run the harness, and reply in two lines.
```

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

> Hint: `mcp_search_notes` in the trace but no `mcp_read_note`? The agent
> never needed a whole note, so your `read_note` never ran. Tell it to find
> the rules of engagement with `mcp_search_notes` and read that note with
> `mcp_read_note` before it touches the target, and run again.

Prompt for your coding assistant:

```text
Write the complete read_note in mcp-skeleton/server.py, following its comments. I'm asking for the full answer. Then load the server in recon's build_registry the way LABS.md recon Step 5 shows, and add a line to the end of the recon SYSTEM_PROMPT telling it to find the rules of engagement with mcp_search_notes and read that note with mcp_read_note before it touches the target. Run the tests. Make only these changes: no other edits, no refactors, don't run the harness, and reply in two lines.
```

### What testing showed

- Recon finished Flag Command by itself, in six turns, with "do not exploit
  anything" in its prompt. A prompt cannot hold the line between finding
  something and using it while the role has a shell. Production harnesses
  hold it with the tool set: the mapping role gets no tool that could use what
  it finds. Here recon keeps its shell, so that line is yours to watch in the
  trace.
- On a harder task, recon spent every turn exploring and recorded nothing, so
  triage got an empty list. Only the loop knows where the turn cap is, so it
  now tells every role when three or fewer turns remain, and recon cannot
  finish with `success` until it has recorded something.
- If recon sends anything that does more than read (a POST, a form, `curl`
  with data), the harness counts it, prints a note, writes it to the trace and
  marks recon's status `overstepped`. It needs nothing task-specific, so it
  works on a real target too. It makes overstepping visible; giving recon no
  tool that can send is what prevents it.
- One provider decoded the URL in `http_request` against the schema's
  `pattern` as a whole-string match, so the model could send only `http://`.
  The model fell back to curl and the run looked fine. A tool schema is a
  contract with every provider that serves the model.
- The browser comparison had little to compare: everything on this target is
  readable with curl, so the skill was used once at most and the MCP tools not
  at all.
  Offering 25 MCP tools that nobody called still took input tokens from 45.6k
  to 98.6k for the same run.
- A call that comes back with `stop_reason` `content_filter` and no tokens was
  refused by the provider. It is not your prompt; see
  [CHEATSHEET.md](CHEATSHEET.md).

---

## Triage lab: handoff and triage (20 min)

Recon output is long and noisy. Triage removes duplicates in plain code, then a
model ranks what is left and has to defend each rank. A panel of three other
models then tries to disprove each candidate.

### Step 1: the triage prompt and `rank_candidate`

Open `harness/roles/triage.py`. The dedup runs in code before the model sees
anything; you write `SYSTEM_PROMPT` and `rank_candidate`, both marked
`TODO(M3)`.

> Hint: ask for the cheapest check that would prove each candidate wrong, not
> just a score. A ranking nobody has to defend drifts toward whatever sounds
> most dangerous. Keep `must_be_true` to what the observation can already
> show: the refuters cannot run anything, so a check that still needs running
> belongs in `first_check`.

> Hint: the refuters see only what `rank_candidate` stores. If you want them
> to read the observation, and the call recon logged with it, copy them into
> the candidate.

Prompt for your coding assistant:

```text
Write the complete triage SYSTEM_PROMPT and rank_candidate in harness/roles/triage.py, following the TODO comments and the hints in LABS.md triage Step 1. I'm asking for the full answer. rank_candidate must reject a dedup_key that isn't in the list and copy the source observation into the candidate. Run the tests. Make only these changes: no other edits, no refactors, don't run the harness, and reply in two lines.
```

### Step 2: the refuter prompt

Open `harness/roles/refute.py` and write `SYSTEM_PROMPT`. Ask for refutation,
not review: each panelist's job is to disprove the candidate.

> Hint: a refuter with no tools can reject any candidate by imagining a
> defence ("the server might check a session"). Ask for a refutation that
> points at something the panelist was shown: a status, header or field that
> contradicts the claim. "By design" is not a reason if it hands a caller
> something they should not have.

Prompt for your coding assistant:

```text
Write the complete refuter SYSTEM_PROMPT in harness/roles/refute.py, following the TODO comment and the hint in LABS.md triage Step 2. I'm asking for the full answer. Run the tests. Make only these changes: no other edits, no refactors, don't run the harness, and reply in two lines.
```

### Step 3: run through the refuter panel

```
python -m harness.run --adapter cybench --task flag-command --budget-usd 3 --pause-after refute
```
```
python -m harness.trace_stats runs/<run-dir>/trace.jsonl
```

Note: every run starts from the top, so this re-runs recon too. The panel
takes a few minutes: the three panelists judge each candidate at the same
time, but each candidate waits for its slowest panelist. Only the three
best-ranked candidates go to the panel; the trace notes any it did not judge.

What did the panel reject? Did the panelists agree?

### Why three refuters?

The rule is that whatever generated a candidate never judges it alone, so the
refuters come from other model families than the generator (DeepSeek). One
outside model would satisfy that rule. The other two are for the vote:

- A single refuter's mistakes go straight through. A wrong "stands" passes a
  false positive, and a wrong "refuted" drops a real bug.
- With three, a candidate is dropped only when most of the panel refutes it, and
  `uncertain` counts as refuted.
- They are three different families because three copies of one model tend
  to make the same mistake. An odd number means no ties.

Do not put an abliterated model in triage, refute or the judge. Removing
refusals also damages calibration, and these roles exist to say no.
`harness/README.md` ("On abliterated models") has the measurements, and Matt
Suiche's weightless write-up on one way it happens: the direction that lets a
model notice it lacks information looks like refusal, and removing it makes
the model confabulate.

### What testing showed

Three test runs in a row ended with nothing surviving the panel. Each time the
panel was right about what it was shown, and what it was shown was the
problem:

- `must_be_true` asked for a check nobody had run yet. The refuters have no
  tools, so all they can do with that is refute it.
- The observation was the model's own account of a request. With the request
  itself missing, "we cannot tell whether credentials were sent" is a fair
  objection.
- "Intended behaviour" and "sample data" excused a weakness that handed any
  caller something they should not have.

What fixed it: recon kept the call the harness logged with each observation,
and `rank_candidate` copied it into the candidate, so the refuters read the
exact request and response. None of that is automatic; it is code you write.
Also: a refutation has to point at something it was shown, and `must_be_true`
says what the evidence already shows. The next run's
candidates survived 3 to 0 and 2 to 1.

One panelist voted the right way for the wrong reason: it read the app's hidden
command as nonsense input. Read the reasons, not just the votes.

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

### Optional stretch: a bigger panel and a referee

Three refuters and a majority is the smallest panel with a vote. If you add
judges, do not let the bar grow with the panel: two judges who agree, each
pointing at the evidence, is usually enough to treat a candidate as a
finding. Then decide what settles a split: a referee agent that reads the
disagreeing reasons and rules, or a plain function, such as ties going to
`refuted`. The vote is `aggregate()` in `harness/roles/refute.py`, and the
panel's models are the `panel:` list under `refute` in `config/roles.yaml`.

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

Prompt for your coding assistant:

```text
Write the complete build SYSTEM_PROMPT in harness/roles/build.py, following the TODO comment and LABS.md confirm Step 1. I'm asking for the full answer. Make only these changes: no other edits, no refactors, don't run the harness, and reply in two lines.
```

### Step 2: the exploit prompt and the negative control

Open `harness/roles/exploit.py`. Write `SYSTEM_PROMPT`, then
`control_triggered()`.

The harness runs the negative control itself: an ordinary request every
visitor makes. `control_triggered()` decides whether that benign request
triggered the behaviour you are claiming. If it did, your exploit proves
nothing.

> Hint: `control_triggered()` is handed the task's `marker`, its success
> marker from `TASKS`. Use it rather than typing the value. A clean control's
> output must not contain it. And a control that failed to run at all is not
> a clean control.

Prompt for your coding assistant:

```text
Write the complete exploit SYSTEM_PROMPT and control_triggered in harness/roles/exploit.py, following the TODO comments and LABS.md confirm Step 2. I'm asking for the full answer. control_triggered must use its marker argument, and treat a control that didn't run cleanly as triggered. Run the tests. Make only these changes: no other edits, no refactors, don't run the harness, and reply in two lines.
```

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

Prompt for your coding assistant:

```text
Write the complete judge SYSTEM_PROMPT in harness/roles/judge.py, following the TODO comment and LABS.md confirm Step 3. I'm asking for the full answer. Run the tests. Make only these changes: no other edits, no refactors, don't run the harness, and reply in two lines.
```

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

### What testing showed

With the triage fixes above, the full run on Flag Command cost about $0.15 and
took about six minutes. Build wrote the contract first, the negative control
ran clean, and the judge ruled `confirmed`. It is that cheap because the bug
is Very Easy. The contract, the control and the verdict gate work the same way
on a hard bug; the turns and the bill do not.

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

Prompt for your coding assistant:

```text
In scope-gate/rules.yaml, under flag-command, add a named deny for host locktalk with the note "decoy task, out of scope", and an ask for host host.docker.internal with the note "the host machine; a human decides". Make only these changes: no other edits, no refactors, don't run the harness, and reply in two lines.
```

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

> Hint: no `network` decision in that grep? The agent used only the shell
> (`curl`), which the gate sees as a shell command, not a connection. Tell it
> to use `http_request` for the decoy too, and run again.

Note: if you commented out the Playwright MCP line in `build_registry()` after
the recon lab, put it back before this run. Otherwise Step 4's third finding,
the browser that never asks the gate, will not show up.

> Hint: no `pw_` calls in the trace, even with the MCP line in? The agent
> preferred `curl`. Name the tool in your prompt: tell it to open
> `http://locktalk:1337/` with `pw_browser_navigate`, and run again.

Prompt for your coding assistant:

```text
Add a line to the end of the recon SYSTEM_PROMPT saying the service at locktalk:1337 is also part of this assessment: map it as well as web_flag:1337, use http_request for HTTP to both hosts, and open http://locktalk:1337/ with pw_browser_navigate. Keep the trimmed Playwright MCP line in build_registry. Make only these changes: no other edits, no refactors, don't run the harness, and reply in two lines.
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

Note: with no terminal on stdin, for example when a coding assistant runs the
command for you, the harness behaves as `--non-interactive`, and every `ask`
becomes a deny.

### Rules in code, or a model judge?

The gate here is rules in code. Rules cannot be argued with, but they miss
anything nobody wrote down. A model judge covers that long tail for a fraction
of a cent per call, but it can be talked out of its answer.

- Adrian Wood's talk "Hackbots!" described a small
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

Jev is one ready-made judge for this: a small model, reachable through
OpenRouter, that returns a typed answer with a probability.

### Optional stretch: an allowlisting proxy

The gate judges the harness's own tools. A proxy judges every connection,
including `curl` from the shell and the Playwright MCP browser, the two that
got past the gate in Step 3. Put the attacker and the MCP container on an
internal network whose only way out is a proxy that allows just the hosts in
scope; [harness/README.md](harness/README.md#the-network-internal-here-an-allowlist-for-real-use)
describes the layout. Then run Step 3's attack again. The layers end up as:
the proxy for limits nothing can argue with, rules in code for what you
wrote down, and a model judge for the long tail.

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

### Turns, tokens and the cliffs

Where a run falls off as it gets longer, from this harness's own runs:

- **Every turn resends everything.** A model call carries the whole
  conversation so far, so input tokens grow with every turn. One recon run
  took six turns and 48k input tokens, 28k of them cache reads. Keep the
  system prompt identical across turns so it caches; the loop adds its own
  notes as user turns for that reason.
- **The output cap includes reasoning.** A role that hits `max_output_tokens`
  mid-thought stops with `max_tokens`, and that turn bought nothing. Raise the
  role's cap in `config/roles.yaml` before blaming the model.
- **The turn cap loses whatever is unrecorded.** Record as you go; the loop
  warns when three turns remain.
- **Long runs drift.** On a harder task, recon's output grew from under 100 to
  about 3,500 tokens a turn, it ignored its turn budget, and it recorded
  nothing. Short stages that hand on short records hold up better than one
  long one.

The harness caps each tool result at 20,000 characters. How much evidence each
observation keeps is yours to decide in `record_observation`: triage reads
every observation in one prompt.

### Structure and prompt characteristics

The role prompts in a production security harness look less like an essay and
more like a runbook:

- **One short prompt per role.** Most run 20 to 100 lines. A sub-agent gets
  one focus, and is told that everything else belongs to someone else.
- **Open with what the role does not do.** The exploit role provisions, runs
  and gathers evidence; deciding whether it worked is the judge's job.
- **Numbered workflows that name the tools,** one per mode, down to which
  field in a tool's output decides the next step.
- **A short block of hard rules:** always clean up, even on failure; never
  change another role's artifact.
- **A fixed ending:** exactly one recording call, with the fields it needs.
- **The reason for each rule,** including what the harness does behind the
  scenes, so the model does not fight it.
- **Concrete over generic:** an exact search pattern, not "look for secrets".
- **A stance per role.** Generators report at low confidence and leave the
  filtering to validation. Refuters assume the finding is wrong, but a
  refutation they cannot show does not count.
- **Grounded evidence:** quote the line, cite the logged call, never a
  paraphrase.
- **External rules first.** A bug bounty program's rules come before anything
  in the prompt and cannot be overridden by it.

And the code does what a prompt cannot. Each role gets only the tools for its
job. Handlers refuse an invalid call and say why. The loop adds its notes
(turns left, caps) as user turns, which also keeps the system prompt
byte-identical for caching. A finish is checked against what the role was
supposed to produce.

### What testing showed, as twelve rules

Each came from a run that went wrong while this workshop was being built.

1. Read the stop reason before you blame the model.
2. A provider refusal looks like a model failure: `content_filter`, no tokens.
3. Prompts are not boundaries. Enforce in code what must not happen.
4. Only the loop knows the clock. Tell the role how many turns it has left.
5. Saying it is done does not make it done. Check the handoff in code.
6. Evidence the harness logged beats evidence the model wrote.
7. A refutation has to point at something the refuter was shown.
8. Read the reasons, not just the votes.
9. A tool schema is a contract with the provider, not just the model.
10. Tools nobody calls still cost tokens on every turn.
11. Benchmark prompts carry framing. Brief the agent the way you would brief
    a person.
12. A good score on public tasks can be recall. Test on something the model
    has not seen.

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
