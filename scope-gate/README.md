# scope-gate/

`rules.yaml` is the scope gate's rulebook. `python -m harness.run` loads it
unless you pass `--scope-rules`, and `harness/gates/scope.py` checks every
action that leaves the sandbox against it.

It ships with the two rules the labs need to reach the target: the target
itself (`web_flag:1337`) and the agent's shell in its attacker container.
Everything else is denied by default.

The scope lab is the `TODO(M5)` in `rules.yaml`. How the gate works, and what
it can and cannot see, is in the docstring at the top of
`harness/gates/scope.py`.
