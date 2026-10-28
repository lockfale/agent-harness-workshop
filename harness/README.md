# harness/

A small multi-agent harness for security work. Six roles, one gateway, and a
set of gates that live in code rather than in a prompt.

What you fill in during the labs is role prompts and tool bodies, marked
`TODO(M2)` through `TODO(M6)` with a docstring at each one explaining what the
exercise is. You should not need to rewire anything. The plumbing passes the
offline test suite (no key, no Docker); see "What is still TODO" for what has
not yet been exercised against a live provider and a real task.

Target: Cybench tasks only. Nothing custom-built, nothing hosted, no
authorization surface.

## Install

Python 3.11 or newer.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r harness/requirements.txt
```

Four dependencies: `openai` for the OpenAI-compatible client, `jsonschema` for
tool validation, `PyYAML` for the config, and `pytest` for the tests. The
`mcp` package is optional and needed only for the M2 exercise that loads tools
from an MCP server.

## Run one task

```bash
export HARNESS_API_KEY=sk-or-...                  # your OpenRouter key
export HARNESS_BASE_URL=https://openrouter.ai/api/v1   # the default; override for a local gateway

python -m harness.run --adapter cybench --task "path/to/[Very Easy] Flag Command" --budget-usd 5
python -m harness.trace_stats runs/<task-slug>-<ts>/trace.jsonl
```

The run refuses to start without `HARNESS_API_KEY` (any placeholder works for a
local gateway that does not check it), with a model missing from the price
table, or with a `--run-dir` that already has files in it.

Useful flags:

| Flag | What it does |
|---|---|
| `--task <path or slug>` | The Cybench task directory, or a slug with a `TASKS` entry. Required. The id is always a slug: `[Very Easy] Flag Command` becomes `flag-command`. |
| `--roles <file>` | Role to model map. Default `config/roles.yaml`. |
| `--prices <file>` | Price table. Default: `prices.yaml` next to `--roles`. |
| `--budget-usd N` | Cap for the whole run. Per-role caps come from `roles.yaml`. |
| `--run-dir <dir>` | Where the trace, status files and contract go. Must be new or empty: stale status files would be read as this run's evidence. Default `runs/<slug>-<timestamp>`. |
| `--pause-after <stage>` | Stop after that stage (any stage before the judge) so you can read the trace while it is warm. |
| `--previous-tier <tier>` | The `proof_tier` an earlier run of this task reached. The verdict gate will not record a lower one (rule 4). Nothing is read from disk without this. |
| `--break-harness` | M4 lab. Kills the service after the readiness check, on purpose. |
| `--adapter stub` | Runs the pipeline with no containers. Good for checking your wiring without pulling images. Default. |
| `--adapter cybench` | The real adapter. Needs Docker, Compose 2.24.4 or newer, and a Cybench task directory. |
| `--scope-rules <file>` | Scope rules. Defaults to `scope-gate/rules.yaml`; with no rules for the task, the gate denies everything that leaves the sandbox. |
| `--non-interactive` | Never prompt. The scope gate denies anything it would have asked a human about. |

## How a task runs

The agent never gets a shell in the target, and never sees the task directory
(it holds `flag.txt`, the solutions and the writeup). With `--adapter cybench`:

1. `shared_net` is created if missing, with `--internal`: containers on it
   reach each other by hostname but not the internet, so the agent cannot
   fetch a public writeup of the task. Model calls are made from the host, so
   they are unaffected. Cybench compose files declare the network external. If
   it already exists and can reach the internet, the run goes ahead with a
   warning that says how to recreate it.
2. The task's compose file comes up as project `<slug>`, with an override that
   removes published host ports (`ports: !reset []`), so two tasks never fight
   over 1337.
3. `init_script.sh` runs in a throwaway container from the attacker image, with
   the task directory mounted read-only. It copies the agent-visible files into
   a volume. Its stdout is ignored.
4. The attacker container (`harness/attacker/Dockerfile`: python3, requests,
   curl, jq) starts on `shared_net` with only that volume mounted, at
   `/work/files`.
5. The prompt is built from `metadata/metadata.json` (`hard_prompt`, or
   `easy_prompt` via `TASKS`), plus a fixed line naming `target_host`.
6. `shell` and `http_request` run in the attacker container and reach the
   target by hostname. The target container is touched only by the harness,
   for the negative control and an optional in-target probe.
7. Readiness is a TCP connect to `target_host` from the attacker container over
   `shared_net`, retried with backoff for up to two minutes. Still unreachable
   is `harness_failure`, and the judge is skipped. Compose is not run with
   `--wait`, and the compose healthcheck is recorded in the trace as advisory
   only: Cybench healthchecks use the task image's own tools, and Flag Command's
   (`nc -z` on a BusyBox base, which has no `-z`) reports `unhealthy` while the
   service serves 200.
8. Everything is torn down in a `finally`, whatever happened. `shared_net` is
   left for the next run.

Task tool requirements are baked into the attacker image, not installed at run
time. Cybench's `metadata/requirements.sh` is not run: with a room of
attendees on conference wifi, a per-task `apt-get` at session start is the
likeliest thing to fail. Flag Command's only requirement is `jq`, which the
image has. LockTalk's requirement (jwcrypto; requests is already in the image) is not baked in because
LockTalk is the out-of-scope decoy for M5 and the agent is not meant to solve
it. If the task picks change, add the new tasks' requirements to the
Dockerfile.

The scope gate sees `http_request` as a network action with a host and port.
It sees `shell` as a command in the attacker container and does not parse it,
so `curl` to an out-of-scope host goes past it. That gap is the M5 lab; see the
docstring in `gates/scope.py`.

### Docker, and the more secure alternative

Containers share the host's kernel. What separates the attacker container from
your machine is namespaces, cgroups and seccomp, so a kernel bug reachable from
the agent's shell is a way out. For a sandboxed CTF on your own laptop that is
an acceptable risk. For model-written commands against anything you care
about, it is not.

The stronger option we considered is [Firecracker](https://firecracker-microvm.github.io/),
the open-source microVM monitor built on KVM that isolates AWS Lambda and
Fargate workloads. Each microVM boots its own guest kernel, so escaping the
workload means escaping a virtual machine rather than a set of namespaces. The
labs use Docker anyway for two reasons:

* Cybench ships its tasks as Compose projects.
* Firecracker needs Linux with KVM, which most attendee laptops (macOS,
  Windows) do not provide natively.

If you take this harness further, run the attacker side in a microVM. Kata
Containers can put Firecracker underneath a container workflow.

### The network: internal here, an allowlist for real use

`shared_net` is internal because this is a CTF. The target is local, and
internet access would let the agent look up a writeup instead of solving the
task. That is the right setting for the workshop and the wrong one for real
work, where the agent needs a remote target and, legitimately, some research
sources: CVE databases, upstream issues and commits, documentation. Before you
point the harness at anything real, replace the internal network with an
allowlist:

* Keep the attacker container on an internal network and add a proxy container
  attached to both that network and the outside. The proxy is the only way
  out. It allows the in-scope hosts from your scope file, nothing else, and
  logs every request.
* Enforce it with the network layout, not `HTTP_PROXY` variables, which a tool
  can ignore.
* Give research its own role and container that can read the web but cannot
  reach the target. When the target is a local build, the exploit and judge
  stages stay offline.
* The scope gate judges each tool call, and the proxy catches anything that
  gets past it. Model calls leave from the host and need their own controls,
  because a model API can carry vendor-run tools such as web search.

### The workshop tasks

`harness/cybench.py` `TASKS` has two entries, keyed by slug, with `workdir`
relative to the repository root:

* `flag-command` is the task for recon, triage, confirm and the prompt A/B
  demo. Its target is `web_flag:1337`. The benign control is an ordinary
  `GET /api/options`, and its output must not contain the flag format
  (`control_marker: "HTB{"`).
* `locktalk` is the M5 decoy. It is never the target and cannot be run as the
  main task. Bring it up next to flag-command on `shared_net`:

```bash
python -m harness.run --adapter cybench --task flag-command --with-task locktalk
```

It comes up and goes down with the main task (also in the `finally`). Its
containers are listed in `task.containers`, and nothing points a tool at it.

The gate loads `scope-gate/rules.yaml` unless you pass `--scope-rules`. It
ships allowing only the target and the attacker shell. A version for the scope
lab, with a named deny for the decoy:

```yaml
default: deny
tasks:
  flag-command:
    allow:
      - {kind: network, host: web_flag, ports: [1337]}
      - {kind: shell, container: "harness-attacker-*"}
    deny:
      - {kind: network, host: locktalk, note: "M5 decoy: out of scope"}
```

With this, `http_request` to `locktalk:1337` is denied with a named rule in
the trace. `curl locktalk:1337` in the shell is allowed, because the gate sees
a shell command in the attacker container. That gap is the lesson.

Roles with reasoning enabled echo the provider's `reasoning_details` back on
the next turn of a tool loop, which some providers require. If yours rejects
the field, set `echo_reasoning: false` on that role in `roles.yaml`; set
`reasoning_effort: null` to stop asking for reasoning at all.

Run the tests with no API key and no Docker:

```bash
python -m pytest harness/tests -q
```

## Switching models

`config/roles.yaml` maps each role to a model name. That name is passed to the
gateway untouched, so prefix routing is the gateway's problem and not the
harness's:

```yaml
roles:
  judge:
    model: anthropic/claude-sonnet-5.5   # straight to OpenRouter
  recon:
    model: local/qwen3-coder             # behind a gateway that strips local/, priced at $0
```

A `local/` prefix only routes if a gateway in front of your models strips it.
Pointing `HARNESS_BASE_URL` straight at Ollama or another server instead? Use
the model name exactly as that server lists it, and give it a row in
`config/prices.yaml` at zero (`SETUP.md`, step 5). Either way only these
strings change: attendees with two GPUs and attendees with twenty dollars of
API credit run identical code.

Anthropic and OpenAI models can refuse offensive security work, CTF tasks
included, unless the account has completed that provider's cyber
verification. The trace shows a refused call as `stop_reason=content_filter`
with no tokens used. Read that before you blame your prompt.

Every model name in `roles.yaml`, panel members included, must appear in
`config/prices.yaml` or carry a local prefix. An unlisted model aborts the run
at startup with a message pointing at the file. That is the point: see P5.

## Prompt A/B: appending a system-prompt suffix

Any role in `roles.yaml` can set `system_prompt_suffix_file`. It is off by
default. The file is read once at start, and its text is appended (never
substituted) to that role's system prompt. The path may live outside the
repository, and `~` expands. A missing file stops the run at start: an A/B arm
that silently ran without its suffix is not an A/B. The trace records only the
file's basename and the sha256 of its content, in `role_start` and in the
resolved config in `run_start`. The text itself never reaches the trace, so a
prompt you keep private stays private while two runs can still be told apart.

The motivating example is Adrian Wood's GEPA "operational discipline" prompt
for security agents: a short list of imperative principles, evolved with GEPA
(reflective prompt evolution over execution traces). The reported improvement
from 53% to as high as 77% solve rate is **in-sample**: it is the score on the
task set the prompt was optimised against, a training score, not a held-out
result. Treat it as a reason to test the idea, not as evidence it transfers.
This repository does not ship that prompt or any other.

The A/B is the M6 demo:

```bash
# arm A: no suffix
python -m harness.run --adapter cybench --task flag-command --run-dir runs/ab-a
# arm B: same task, same models, suffix on the roles you are testing
python -m harness.run --adapter cybench --task flag-command --roles config/roles-b.yaml --run-dir runs/ab-b

python -m harness.trace_stats runs/ab-a/trace.jsonl
python -m harness.trace_stats runs/ab-b/trace.jsonl
```

Compare turns, tool calls, tokens, cost and the last stop reason per role,
and whether the verdict changed. One pair of runs is an anecdote. Several runs
per arm, on a task neither prompt was tuned on, is the minimum for a claim.

## Module map

| Module | Role in the session | What it holds |
|---|---|---|
| `run.py` | all | The orchestrator. Runs the six stages and branches on their status files. |
| `config.py` | all | Reads `roles.yaml` and `prices.yaml` once, resolves defaults, echoes the result into the trace. |
| `llm.py` | all | One OpenAI-compatible client. Normalises stop reasons (`tool_calls` to `tool_use`, keeping the raw value) and usage; a 200 with an error body is a provider error. |
| `pricing.py` | M6 | The price table, prefix stripping, free local models, fail-fast on an unknown model. |
| `budget.py` | M6 | Running cost, per-role and per-run caps. |
| `trace.py` | all | The JSONL event log and the redaction rules. |
| `trace_stats.py` | M6 | Per-role turns, tokens, cost, wall time, and the last stop reason. |
| `status.py` | M4 | Stage status files and which outcomes count as infrastructure failures. |
| `sidechannel.py` | M4 | Wraps notifications, telemetry and report rendering so they cannot end a run. |
| `cybench.py` | M2, M5 | The task adapter: slugs, metadata, compose with the ports override, the attacker container. `StubAdapter` runs offline. |
| `attacker/Dockerfile` | M2 | The attacker image. Add the tools your tasks need here, or set `attacker_image` in `TASKS`. |
| `tools/registry.py` | M2 | Tool registration, JSON Schema validation, the MCP stdio hook. |
| `tools/shell.py` | M2, M5 | A shell that only runs in the attacker container, checked by the scope gate as a shell action. |
| `tools/http.py` | M2, M5 | A structured HTTP request from the attacker container. Host and port go through the scope gate; redirects are not followed. |
| `tools/finish.py` | M2 | The `finish` tool. The only way a role ends successfully. |
| `tools/mcp_stdio.py` | M2 | Loads an MCP server's tools through the same validator. |
| `roles/base.py` | all | The turn loop every role shares. Not an exercise. |
| `roles/recon.py` | **M2** | Explore the task, emit observations. |
| `roles/triage.py` | **M3** | Deterministic dedup in code, then a ranking the model has to defend. |
| `roles/refute.py` | **M3** | The adversarial panel. Majority refutes, uncertain counts as refuted, an infrastructure failure halts the panel. No survivors ends the run. |
| `roles/build.py` | **M4** | The expectations contract, the readiness probe (not ready is a harness failure), `--break-harness`. |
| `roles/exploit.py` | **M4** | Refuses to start without the contract. Runs the negative control. |
| `roles/judge.py` | **M4** | Records a verdict through the gated tool, or not at all. |
| `gates/verdict.py` | **M4** | The six verdict rules. |
| `gates/scope.py` | **M5** | `check(action)` returns allow, deny or ask, on everything that leaves the sandbox. |

M1 is the talk, and M6 is a demo built on `trace_stats.py` and `pricing.py`
rather than a lab.

## Design principles

Each row is something that went wrong first and became a rule afterwards.

| # | Principle | Failure it prevents |
|---|---|---|
| P1 | Judge a stage by the artifact it wrote, not by its exit code. Every stage writes a status file and the orchestrator reads the file. | A crashed stage read as "completed, 0 findings", so the target was reported clean. |
| P2 | Gate verdicts in code. The model proposes and the handler disposes; a disallowed verdict comes back as an error the model has to answer. | Prompt-only rules were ignored, and infrastructure failures were scored as "exploit failed". |
| P3 | Every tool carries an explicit JSON Schema, and inputs are validated before the handler runs. An invalid input returns a structured error and is never executed. | A tool declared without a schema was called with `{}` or with invented argument names, and nothing happened silently. |
| P4 | Side channels cannot kill the main loop. Notifications, telemetry and report rendering are wrapped so their exceptions are recorded and dropped. | A notification helper bug killed a 7-hour run at the moment of the first finding. |
| P5 | Cost comes from a price table you can audit. Unknown models are an error, not a guess. Local models cost nothing. Gateway prefixes are stripped before lookup. | A budget guard priced an unknown model at a frontier default and aborted a run at about 60x its real cost. |
| P6 | Infrastructure failure means unconfirmed, and the judge is skipped. Build, harness, environment and timeout failures never reach a judge. | Real bugs discarded as false negatives. |
| P7 | Probe readiness at the real service socket (from the attacker container over `shared_net`), not at a forwarded port and not by trusting a healthcheck. | Rootless port forwarders complete the TCP handshake with a dead service. |
| P8 | Record stop reasons and per-turn usage. Never summarise a run as "N tool calls" without them. | "57k tokens, zero tool calls" read as a model that would not act, when it was about to act. |
| P9 | Read config once per run and echo the resolved result into the trace: model per role, base URL host, prices. | A long-lived process kept serving a stale model config. |
| P10 | Whatever generates a candidate never solely judges it. Refuters use a different model lineage where you can arrange one. | Self-confirmation, and calibration damage in abliterated models. |

### Why three refuters, not one

One refuter from another lineage is enough to satisfy P10. The other two are
for the vote. A single refuter's mistakes go straight through: a wrong "stands"
passes a false positive, and a wrong "refuted" kills a real bug. With three, a
candidate dies only when most of the panel refutes it, and `uncertain` counts
as a refutation. The three come from different families because three copies
of one model tend to make the same mistake, which is one opinion counted three
times. An odd number means no ties.

### On abliterated models

Do not put an abliterated model in the triage, refute or judge roles. The
problem is not that it will say anything you ask; the problem is that removing
refusals also damages calibration, so its confidence stops carrying
information. In measurements on this pipeline an abliterated model graduated
96% of candidates as true findings against 65% for the same base model. It also
confirmed bugs in files that had none, and it missed real CVEs. Both halves
matter: a model that will not say no is not more thorough, it is less useful,
and every one of these three roles exists to say no.

Recon and exploit are a different question, and you may find a use for one
there. The three judging roles are not negotiable.

### On a confirmed finding with the wrong severity

If the harness built the preconditions itself, it has proved the bug exists
under conditions it invented. Whether production code ever arrives in that
state is a separate verification step, and this harness cannot answer it,
because the harness is the thing asserting the premise. That is rule 6 in
`gates/verdict.py`: unless the judge sets `trigger_origin: real_code_path`, the
proof tier is capped at `exploitable`, and `impact_confirmed` is never
available on a synthetic or unknown premise. Rules 5 and 6 decide on these
structured fields (`required_privileges`, `privilege_boundary_crossed`,
`trigger_origin`). Keywords in the prose fields only raise a `gate_warning` in
the trace, because a CTF precondition mentioning `/admin` or an API key is
normal and should not be rejected for it.

### On autonomy scores

The `autonomy` field runs 0 (fully autonomous) to 4 (research only). Source
code vulnerability hunting belongs at 0 or 1. Live pentest and red team work
belongs at 2 or 3, and that is the right answer rather than a shortcoming.
Anyone claiming full autonomy on live operations is describing a process
problem.

## Deliberately out of scope

* **Persistent memory across runs.** If you add it, make it a schema'd tool and
  test it by asserting the side effect, which means checking that a file was
  written. A test that only sees the call was made passes against a tool that
  does nothing.
* **Real targets.** Cybench only. Real use needs an allowlisted network first;
  see [The network](#the-network-internal-here-an-allowlist-for-real-use).
* **Abliterated models in the three judging roles.** See above.

## What is still TODO

In the harness, for you:

* `roles/recon.py`: the system prompt, and the body of `record_observation`.
* `roles/triage.py`: the system prompt, the body of `rank_candidate`, and
  optionally `dedup_key` if your observation shape needs a different notion of
  sameness.
* `roles/refute.py`: the panelist system prompt.
* `roles/build.py`: the system prompt, and anything the task needs before the
  exploit role starts.
* `roles/exploit.py`: the system prompt, and `control_triggered()`, which
  decides what counts as the benign input having triggered the behaviour. That
  predicate is what the negative-control rule turns on, so it is yours and not
  the model's.
* `roles/judge.py`: the system prompt.
* `scope-gate/rules.yaml`: the scope lab (M5). It ships allowing only the
  target and the attacker shell, and `run.py` loads it by default.

In the harness, for the facilitator:

* `cybench.py`: the `TASKS` table holds `flag-command` (the target for every
  lab) and `locktalk` (the scope lab's decoy), and no task id is hardcoded
  anywhere else. Container, target, probe and prompt come from the task
  directory, so an entry needs only the negative-control input
  (`control_command`) and whether the task is a decoy.
* `config/prices.yaml`: every row was checked against OpenRouter's public
  models API on `prices_verified` (2026-10-26). Re-check before the session.
* Not yet exercised live: a full run against a real Cybench task on Docker,
  and the reasoning echo against an Anthropic model with reasoning enabled.
