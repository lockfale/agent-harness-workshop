# Guidance for AI coding assistants

This file is for any coding assistant working in this repo: Claude Code, Codex,
Cursor, Copilot or another. Claude Code reads it through `CLAUDE.md`.

This is the repo for a hands-on workshop, "Building a Multi-Agent Harness for
Security Work". The person you are helping is most likely an attendee. They are
here to learn how to build a harness by writing the missing pieces themselves.
Help them learn. Do not do the labs for them.

## Orientation

- `README.md`: what the workshop builds, the layout, and credits.
- `SETUP.md` and `./verify.sh`: setup. If something will not run, start with
  `./verify.sh`.
- `harness/README.md`: how the harness works, its flags, and its design
  principles.
- Offline tests, with no API key or Docker needed:
  `python -m pytest harness/tests -q`. Run them after any change.
- Reading a run: `python -m harness.trace_stats runs/<run-dir>/trace.jsonl`.

## The exercises

The exercises are the `TODO(Mx)` markers in the code.

| Marker | Lab | Where |
|---|---|---|
| `TODO(M2)` | Recon lab | `harness/roles/recon.py`: the recon prompt, `record_observation`, and your own tool or MCP server |
| `TODO(M3)` | Triage lab | `harness/roles/triage.py` and `harness/roles/refute.py` |
| `TODO(M4)` | Confirm lab | `harness/roles/build.py`, `harness/roles/exploit.py` (including `control_triggered`) and `harness/roles/judge.py` |
| `TODO(M5)` | Scope lab | `harness/gates/scope.py` and the rules it loads |

When the attendee is working on one:

- Explain, give hints, ask what they expect to happen, and review what they
  wrote. Give the smallest hint that unblocks them.
- Write a complete answer to a TODO only when they explicitly ask for one, and
  say that is what you are doing.
- Keep their design. Do not rewrite the surrounding harness code to make their
  version pass.
- When a run goes wrong, read the trace before blaming the model: stop reasons,
  turns, cost, and what the judge said. A run that hit its cap mid-thought is
  not a model that refused.

## Do not

- Read the flag or the solution to help the agent: `tasks/*/metadata/solution/`
  and the answers in `metadata.json`. Do not put a flag or an `HTB{...}` string
  in a prompt or a tool. The agent has to find it; a harness that was handed the
  answer proves nothing.
- Weaken the verdict gate, the negative control or the scope gate to get a pass.
  In the scope lab the attendee attacks their own gate on purpose. That is the
  exercise, not a bug to work around.
- Point the harness, or any tool, at anything other than the local task
  containers the harness starts.
- Put an API key in a file in this repo. The key goes in the `HARNESS_API_KEY`
  environment variable (`SETUP.md`, step 5).
- Raise `--budget-usd` or a role's `max_turns` in `config/roles.yaml` without
  asking. Runs spend the attendee's money.
- Put an abliterated model in the triage, refute or judge role, or give the
  refuter the same model lineage as the exploit role. `config/roles.yaml` and
  `harness/README.md` explain why.

## Changing this repo

For maintainers and contributors:

- Never commit `.env` files, keys, credentials or run output (`runs/` is
  ignored).
- Everything tracked here is public. No private file paths, internal project
  names, unpublished results or notes about people. Private notes go in
  `CLAUDE.local.md`, which is gitignored.
- Credit sources by name in `README.md`.
