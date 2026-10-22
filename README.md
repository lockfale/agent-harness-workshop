# Building a Multi-Agent Harness for Security Work

Hands-on workshop. You will build a multi-agent harness from scratch, with
separate agents handling recon, triage, exploitation and reporting, then find
out where these systems fail and why.

**[un]prompted — online, October 27–29, 2026**

## What you'll build

- A harness that routes different roles to different models, because the model
  that generates a finding must never be the one that judges it
- Your own MCP server, wrapping your documentation, a tool you already run, or
  an API
- A triage stage, fed with real output from an agent campaign against an open
  source C++ library: 28 findings, no answer key
- A judge with a working proof of concept and a negative control that must fail
- A scope gate that adjudicates every verification call as allow, deny or ask,
  then you try to break it

## Before the session

Read **[SETUP.md](SETUP.md)** and run `./verify.sh`. Do this a few days early,
not on the morning. No class time is spent on installation.

## Repository layout

| Path | Contents |
|---|---|
| `mcp-skeleton/` | MCP server starting point: transport, schema, one worked tool |
| `harness-template/` | Dockerfile and driver stub with sanitizer flags set |
| `scope-gate/` | allow / deny / ask skeleton |
| `config/` | Gateway config with model-name prefixes per backend |
| `lab-data/` | Findings set used in the triage module |

> **Status: scaffold.** Lab material is being filled in ahead of the session.
