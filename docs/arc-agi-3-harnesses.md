# ARC-AGI-3 harnesses and this one

Background reading. Here you'll find how the agent harnesses built for the
ARC-AGI-3 competitions compare with the design of the harness in `harness/`,
and where each comparison shows up in the labs. As of 2026-10-27.

**The short version.** The strongest ARC-AGI-3 harnesses agree with this one on
the main point. Never trust a candidate until a deterministic check confirms
it, and decide in code what counts as real progress. They differ in three
places:

- **Extra judging roles** are worth less when the check is exact.
- **Volume belongs only where checking is free.** In ARC-AGI-3, every live
  action costs points.
- **When stuck, change the method**, not just the model.

## What ARC-AGI-3 is

ARC-AGI-3 is the [ARC Prize Foundation](https://arcprize.org/)'s interactive
benchmark. An agent plays unfamiliar turn-based games with no instructions and
has to work out the rules by acting.

There are three pools of results, and they can't be compared:

- **The 2025 Preview Agent Competition:** 3 private games, July to August
  2025.
- **ARC Prize 2026 on Kaggle:** hidden games, run offline on a single GPU.
  Results come December 4, 2026.
- **The community leaderboard:** 25 public games, with self-reported scores.
  The public set is "intentionally easier" than the private one.

Each level scores `min(1.15, human actions / agent actions)²`, so every action
an agent spends costs it points. At launch, frontier models with no harness
scored between 0.2% and 0.5%.

| Harness | Result | Approach |
|---|---|---|
| [StochasticGoose](https://github.com/DriesSmit/ARC3-solution) (Tufa Labs) | Preview 1st, 12.58% | Online CNN predicts which actions change the frame. No LLM |
| [Blind Squirrel](https://github.com/wd13ca/ARC-AGI-3-Agents) | Preview 2nd, 6.71% | State graph, loop pruning, ResNet value model |
| [Graph explorer](https://arxiv.org/abs/2512.24156) | Preview 3rd, 3.64% | No training; systematically tries untested actions |
| [The Duck](https://github.com/Tufalabs/duck-harness) (Tufa Labs) | Kaggle Milestone 1, 1st | A local Qwen 27B writes Python in a REPL |
| [Duck fork](https://github.com/da-fr/arc-agi-3-solution) (Daniel Franzen) | Kaggle Milestone 2, 1st | The Duck plus a value-based compute scheduler and no-op guards |
| [Arcgentica](https://github.com/symbolica-ai/ARC-AGI-3-Agents/tree/symbolica/arcgentica) (Symbolica) | Community 36.08% | An orchestrator with tool-restricted subagents |
| [DreamTeam](https://arxiv.org/abs/2605.09650) (NVIDIA) | Community 38.1%, $18,000 | Six fixed roles share a world model |
| [OPINE-World](https://arxiv.org/abs/2607.01531) | Community 78.4%, $1,040 | Program synthesis from counterexamples, plus Bayesian exploration |
| [baseline1](https://github.com/astroseger/arc-3-agents-baseline1) (Sergey Rodionov) | Community 99.0%, $400 | Builds a Python simulator of the game and checks it by replay |
| [Retrodict](https://github.com/ryanbbrown/Retrodict) | Community 99.9%, $654 | A hypothesis must reproduce the log before it gets live actions |
| [Tycho](https://github.com/NIMI-research/Tycho) | Community 100.0%, $2,986 | An actor plus a world-model builder |

## Choice by choice

| This harness | What the competition harnesses do | Verdict |
|---|---|---|
| **Verdicts only through a gated tool**, with the rules in code (`gates/verdict.py`) | OPINE-World's verifier is a deterministic comparator, not an LLM. When a prediction and the replay buffer disagree, that disagreement becomes a counterexample and stops the plan. | Agree |
| **Expectations contract**, written before the exploit runs | Retrodict attaches an expected result to every planned action and halts at the first mismatch. | Agree; theirs is finer-grained |
| **Negative control run by the harness**, with `control_triggered()` deciding what counts | "Change is not progress": status bars are masked before hashing, a 4-cell border is ignored so a ticking timer doesn't count as progress, and "board changed" is reported separately from "level completed". | Same lesson |
| **The healthcheck is advisory**; readiness is probed at the real socket | A signal that moves is not the thing you care about. | Agree |
| **A timeout or infrastructure failure is unconfirmed, never refuted** | Retrodict: "a search timeout is never evidence that no solution exists". The graph explorer's official run looped because actions that triggered a reset were never marked as tested. That was a harness bug, and the agent took the blame. | Agree |
| **Deterministic dedup** before the triage model | States are hashed with the HUD masked. Same-color shapes are grouped into buttons, which cuts the click space about 100x. Object types that never cause a change stop being clicked. | Agree |
| **Refuter panel** across model lineages | Mixed evidence. DreamTeam's six roles gained 2.3 points for $18,000. forge's best run had its arbiter switched off. Tycho's separate world-model builder helped by about 3 points. | A panel earns its cost where the check is weak, not where it is exact |
| **Roles separated by tools**: the judge only has `record_verdict` | Arcgentica's theorists get no action tool. Its testers get "a tight budget to confirm or deny a hypothesis". Its orchestrator "never touches the game directly". | Agree |
| **Recon emits observations**; stages hand off through files | Subagents return "short text summaries, not raw data". Memory lives in files, with every mechanic marked as checked or assumed. | Agree |
| **The task directory is never mounted** in the attacker container | Rodionov audited and closed every route by which game information could leak to the agent. | Agree |
| **Attacker container plus a scope gate** | Tycho runs model code in a container with networking disabled. | Agree |
| **Dollar and turn caps** | Counted actions. Arcgentica splits about 800 actions across its subagents. OPINE-World stops a level after 500 actions without progress. Franzen's scheduler moves compute toward games that are making progress. | Gap: no counted action cap here |
| **A fixed model per role** | They escalate the method. After 300 actions on a level, Retrodict's agent must build a simulator, verify it against every frame, and search it. | Gap: no escalation here |
| **Stop reasons, per-turn usage, reasoning echo** | Franzen: dropping retained thinking "leads to a large regression". | Agree: reasoning continuity is part of the harness |
| **A per-role prompt suffix for an A/B** | Extras often lose. The Duck's hand-crafted tools hurt. Franzen did better with structured world-model updates switched off. forge's best run disabled every add-on. | Agree: every addition must beat its absence |

## What the competitions sharpen

1. **The harness decides whether a model can score at all and what it costs.
   The model sets the ceiling.** Harnesses take models from almost nothing to
   78 to 100% on the public games. Inside a working harness, though, model
   upgrades beat harness variations. In Tufa Labs' words, cost "is mostly
   dictated by the harness", while solvability depends on the model.
2. **Public scores overstate generality.** Entries score 99 to 100% on the
   public games, but the top of the hidden-game board is about 75. One harness
   took Opus 4.6 from 0% to 97.1% on one game and left it at 0% on another.
   That's the same in-sample problem as the GEPA result in
   [harness/README.md](../harness/README.md#prompt-ab-appending-a-system-prompt-suffix).
3. **Test against what you already have before acting live.** Retrodict's
   rule: "Only a hypothesis that survives the log earns real actions." In a
   pentest, a live action costs noise, risk and scope exposure.
4. **Volume only pays where checking is free.** On ARC-AGI-1, accuracy rose
   about 3% for each doubling of samples
   ([Greenblatt](https://blog.redwoodresearch.org/p/getting-50-sota-on-arc-agi-with-gpt)),
   because every sample could be checked offline. The interactive leaders
   minimise live actions instead.
5. **Search needs an exact evaluator.** The harnesses that search do it inside
   a simulator they have already verified against the log, never over
   strategies a model scores.

## Where this shows up in the labs

- **Recon:** tools are restricted by role, and no task information leaks into
  the agent's container.
- **Triage:**
  - Reduce the data in code before any model sees it.
  - After the refuter runs, ask whether it killed anything the negative
    control would not have.
- **Confirm:**
  - `control_triggered()` is your answer to "change is not progress". A timer
    can change the frame while nothing happened.
  - Retrodict's per-action expectations are the contract applied one step at
    a time.
  - `--break-harness` shows a harness failure that would otherwise be scored
    against the agent.
- **Scope:** a network-disabled container is the bluntest network-level
  control: total containment, but no target either. Counted action caps limit
  the blast radius regardless of price.
- **Closing demo:**
  - Measure every addition against running without it.
  - Read the stop reasons.
  - Distrust any score from the same tasks you tuned on.

## Caveats

- Final ARC Prize 2026 Kaggle results come December 4, 2026. The two current
  hidden-set leaders have not published write-ups.
- Community leaderboard scores are self-reported, on the easier public set.
- Further sources:
  - [ARC-AGI-3 technical report](https://arxiv.org/html/2603.24621v2)
  - [Preview 30-day learnings](https://arcprize.org/blog/arc-agi-3-preview-30-day-learnings)
  - [Milestone 1 results](https://arcprize.org/blog/arc-prize-2026-milestone-1)
  - [Franzen's write-up](https://github.com/da-fr/arc-agi-3-solution/blob/main/WRITEUP.md)
  - [Tufa Labs on the Duck](https://tufalabs.ai/research/duck-harness/)
  - [baseline1 ablation](https://arxiv.org/abs/2607.15439)
  - [Tycho paper](https://arxiv.org/abs/2607.28287)
