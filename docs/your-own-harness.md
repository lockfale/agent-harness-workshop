# Taking the harness to your own work

The labs run against Cybench because a CTF has no authorization question: the
target is a container you started, on a network that cannot reach anything
else. Real work has one. This page is about what changes when the harness
points at something you were given permission to test, for red team and
penetration testing, bug bounty, or blue team work.

Everything here assumes you hold that permission in writing. The harness can
enforce a scope. It cannot grant one.

## Start from the authorization, not the target

Every real engagement has a document that says what you may touch:

| Work | The document |
|---|---|
| Red team, penetration test | The signed rules of engagement and scope |
| Bug bounty | The program's published scope and policy page |
| Blue team | Your organization's approval from the system owner, and its data-handling policy |

Write the scope gate's rules from that document, line by line, in the format of
[scope-gate/rules.yaml](../scope-gate/rules.yaml):

* `default: deny`, always. Anything the document does not name is out.
* A named `deny` for every asset the document calls out of scope. The default
  already denies them; a named rule puts the reason in the trace.
* `ask` for anything the document is silent on. Do not decide on the
  client's behalf.
* Put the document's date or version in each rule's `note`, so every decision
  in the trace says which authorization it came from.

Scope changes. When the document does, rewrite the rules from it, then attack
your own gate again the way the scope lab does.

## What changes for all three

**The target adapter.** `harness/cybench.py` brings Cybench containers up and
down. For your own target, write an adapter with the same three methods
(`load`, `start`, `stop`; see `CybenchAdapter`). For a remote target, `start`
and `stop` manage only your attacker container, and `load` returns the task
with its `target_host`, `target_port` and prompt.

**The network.** Replace the internal network with an allowlisting proxy before
anything real. [harness/README.md](../harness/README.md#the-network-internal-here-an-allowlist-for-real-use)
describes the layout. The scope gate judges each tool call; the proxy catches
what gets past it. Two layers, because each misses things the other catches.

**Isolation.** A container is the workshop's sandbox, not a security boundary.
Run the attacker side in a microVM for real work; see the Docker section of
[harness/README.md](../harness/README.md#docker-and-the-more-secure-alternative).

**No flag.** Cybench ends with a string you can check. Real work does not, so
the confirm stage carries the whole burden: an expectations contract written
before anything is tried, a negative control the harness runs itself, and a
proof tier the judge has to justify. A confirmed finding can still carry the
wrong severity if the harness built its preconditions; see
[harness/README.md](../harness/README.md#on-a-confirmed-finding-with-the-wrong-severity).

**Readiness.** Before the exploit runs, the harness checks that the target is
up. On a real target there is no shared task network and no way to probe from
inside the target, so probe from the attacker container through the same path
the exploit will use, proxy included. That is real evidence the service
answered. A port answering somewhere else, such as on your own host, is not.

**A human in the loop, on purpose.** Source code review can run nearly
unattended. Live testing should not; see
[On autonomy scores](../harness/README.md#on-autonomy-scores). Use
`--pause-after` to review each stage before the next one runs, and leave `ask`
rules pointed at a person. `--non-interactive` turns every `ask` into a deny,
which is the safe way to fail.

**Prompts are not boundaries.** In the workshop's own test runs, a recon prompt
that said "do not exploit anything" did not stop the model from doing exactly
that. Anything the authorization forbids has to be enforced in code: the gate,
the proxy, the tool's own checks. The harness counts recon's state-changing
requests and marks the stage `overstepped`, which makes the problem visible;
for live work, give the mapping role tools that cannot send at all.

**Models and data.** Traces hold whatever the target returned, which on real
work means client data. Treat a run directory as engagement evidence, and
check your model provider's data policy (OpenRouter has per-account privacy
settings) before any of it leaves your machine. Some providers refuse offensive
security work without their cyber verification; see
[SETUP.md](../SETUP.md#1-an-api-key-with-access-to-multiple-models--required).
Keep abliterated models out of the judging roles for the reason in
[harness/README.md](../harness/README.md#on-abliterated-models).

**Caps.** The harness caps dollars and turns per role and per run. It does not
count requests or enforce time windows. If your authorization sets either, add
the check to the gate or the proxy before the first run.

## Red team and penetration testing

* Review each phase. Run with `--pause-after recon`, read the observations, and
  only then let triage and the confirm stages run.
* The trace is your activity log. Keep it with the engagement records. If the
  rules of engagement ask you to share your source address with the client's
  defenders, the proxy is where all your traffic leaves from.
* Enforce prohibited actions in code. Denial of service, brute force beyond a
  few guesses, and anything outside the testing window are rules, not
  suggestions. The sample in
  [mcp-skeleton/notes/rules-of-engagement.md](../mcp-skeleton/notes/rules-of-engagement.md)
  is a starting point for the agent's reading, not a substitute for the gate.
* Prove and stop. The confirm stage shows the minimum that demonstrates the
  finding. Pulling more data to make it look bigger is out of scope on every
  engagement worth having.

## Bug bounty

* The program's scope page becomes your rules. Third-party services an in-scope
  app links to are usually out of scope even though the app reaches them; name
  them as denies.
* Programs commonly limit request rates and automated scanning. The gate does
  not rate-limit, so add a limit before you point the harness at a program.
* Test with accounts you created. Never touch another user's data beyond what
  proves the issue, and stop there.
* Read the program's policy on automated and AI-assisted testing and reports.
  Some require you to disclose it; some do not accept it.
* A person checks every finding, looks for duplicates, and writes and submits
  the report. Never connect the judge's verdict to a submission API. The
  verdict's severity is a starting point; the program scores impact on its own
  scale.

## Blue team

The roles map onto defensive work with the same rule: whatever produces a
conclusion never judges it alone.

| Role | In defensive work |
|---|---|
| recon | Gathers context, read-only: alerts, logs, asset and identity data. An MCP server with read-only credentials is the recon lab's pattern |
| triage | Deduplicates alerts in code, then ranks them by what the evidence shows |
| refute | False-positive elimination. Each panelist tries to explain the alert away, from the logs it was shown |
| build, exploit | For detection engineering: reproduce the behaviour in a lab with your emulation tooling. The negative control is ordinary activity that must not fire the detection |
| judge | Decides whether the detection fires and the alert is real, from the recorded evidence |

* Response actions (isolating a host, disabling an account) change production.
  Make them `ask` rules, never `allow`.
* Evaluate on your own data. Public datasets are in the models' training data:
  [bots-bench](https://botsbench.com) found models answering public BOTSv3
  questions with no tools at all, and far fewer on a blinded set. A good score
  on public data can be recall, not skill.

## Before the first real run

- [ ] Written authorization, current, and you have read all of it.
- [ ] Scope rules written from it, `default: deny`, and attacked by you the way
  the scope lab attacks them.
- [ ] An allowlisting proxy, with the attacker on an internal network.
- [ ] `ask` reaches a person, or every `ask` is acceptable as a deny.
- [ ] Request and time limits enforced if the authorization sets them.
- [ ] Budgets and turn caps set, and you know how to stop a run.
- [ ] Your provider's data policy checked for what the traces will contain.
- [ ] Judging roles on models from a different lineage than the generators,
  and none of them abliterated.
- [ ] A person reviews every finding before it leaves your hands.
