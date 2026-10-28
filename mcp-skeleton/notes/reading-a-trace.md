# Reading a trace (sample)

A sample note about diagnosing a run before blaming the model.

- Start with the stop reason of each turn. `max_tokens` means the model was
  cut off mid-thought, not that it refused. `tool_use` means it was about to
  act.
- Count turns against the role's cap. A role that ended at its cap ran out of
  room; it did not decide it was done.
- Check cost per role. A budget breach ends the run wherever it happens.
- Read what the judge said, and which verdict rule rejected what.
- `python -m harness.trace_stats runs/<run-dir>/trace.jsonl` prints most of
  this in one place.
