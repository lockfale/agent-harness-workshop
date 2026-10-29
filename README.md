# Building a Multi-Agent Harness for Security Work

Hands-on workshop. You will build a multi-agent harness from scratch, with
separate agents handling recon, triage, refutation, building, exploitation and
judging, then find out where these systems fail and why.

**[un]prompted — online, October 27–29, 2026**

Slides: [docs/workshop-slides.pdf](docs/workshop-slides.pdf). Every step and command is in [LABS.md](LABS.md).

## What you'll build

- A harness that routes different roles to different models, because the model
  that generates a finding must never be the one that judges it
- Your own MCP server, wrapping your documentation, a tool you already run, or
  an API
- A triage stage downstream of your recon agent that ranks raw agent output
  for validation, and asks you to defend the ranking
- A judge with a working proof of concept and a negative control that must fail
- A scope gate that adjudicates every verification call as allow, deny or ask,
  then you try to break it

## Before the session

Read **[SETUP.md](SETUP.md)** and run `./verify.sh`. Do this a few days early,
not on the morning. No class time is spent on installation.

## Repository layout

| Path | Contents |
|---|---|
| `harness/` | The harness: roles, gates, tools and offline tests. The labs are the `TODO` markers in it; [harness/README.md](harness/README.md) maps them |
| `config/` | `roles.yaml` (which model plays each role) and `prices.yaml` (what each model costs) |
| `tasks/` | The two Cybench tasks the labs run against, copied from upstream |
| `docs/` | [Taking the harness to your own work](docs/your-own-harness.md) (red team, bug bounty, blue team), and how the ARC-AGI-3 competition harnesses compare to this one |
| `scope-gate/` | `rules.yaml`, the scope gate's rules. Loaded by default; the scope lab edits it |
| `mcp-skeleton/` | A small MCP server for the recon lab: stdio transport, one worked tool, one for you to write |
| `lab-data/` | Placeholder, not used yet |

> **Status: scaffold.** Lab material is being filled in ahead of the session.

## Credits

**[Griffin Francis](https://github.com/aussinfosec)** and
**[Adrian Wood](https://github.com/5stars217)**, for inspiration, consultation, and
testing these ideas out.

Adrian's talk "Hackbots! Preparing for, and using offensive AI capabilities"
(UNC Charlotte Cybersecurity Symposium, October 2026) is the source of the risk
cascade (cheap models for recall, static analysis for precision, large models
for judgment), MCP composition, tokenomics as a risk metric, and a scope judge
that answers allow, deny or ask. Adrian's GEPA "operational
discipline" prompt is why the harness can A/B a per-role prompt suffix. Its
reported improvement is in-sample; see
[harness/README.md](harness/README.md#prompt-ab-appending-a-system-prompt-suffix).

Sources:

- [Cybench](https://github.com/andyzorigin/cybench), Stanford CRFM (Zhang et
  al., [arXiv:2408.08926](https://arxiv.org/abs/2408.08926)), Apache License
  2.0. The two tasks in `tasks/` are copied from it; see
  [tasks/README.md](tasks/README.md).
- Hack The Box, Cyber Apocalypse 2024: the original Flag Command and LockTalk
  challenges.
- ARC-AGI-3 competition harnesses ([ARC Prize](https://arcprize.org/)). An
  analysis of how they win shaped the verification design here: orchestration
  alone can lift a model's success rate several-fold, but only around a real
  verifier, because search converges on the truth only when the verifier
  contains it.
- GEPA: Agrawal et al., "GEPA: Reflective Prompt Evolution Can Outperform
  Reinforcement Learning", [arXiv:2507.19457](https://arxiv.org/abs/2507.19457).
- [scopebench](https://scopebench.ai/benchmarks), for the scope lab.
- Matt Suiche's [weightless](https://github.com/msuiche/weightless) and its
  [write-up](https://www.msuiche.com/posts/autoresearch-abliteration-without-redistributing-the-model/):
  removing refusals can also remove the model's sense that it lacks
  information, and a benchmark that scores delivery cannot see the
  difference. See
  [harness/README.md](harness/README.md#on-abliterated-models).
- freed0m's
  [red team bench](https://betheadversary.com/posts/dgx_spark_red_team_bench/):
  every model tested was willing to help by default, so willingness tells you
  nothing and accuracy is what separates them.
