# tasks/

The Cybench tasks the labs run against, copied unmodified so one clone has
everything. The full Cybench repository is about 3 GB; these two directories
are about 1.2 MB.

| Directory | Used in | Upstream path |
|---|---|---|
| `flag-command/` | M2, M3, M4, M6 demo | `benchmark/hackthebox/cyber-apocalypse-2024/web/[Very Easy] Flag Command` |
| `locktalk/` | M5, as the out-of-scope server | `benchmark/hackthebox/cyber-apocalypse-2024/web/[Medium] LockTalk` |

Source: [Cybench](https://github.com/andyzorigin/cybench) (Stanford CRFM),
commit `ca483c6443f17a20ba7d6bac3d274859a1721e51`, Apache License 2.0, included
here as `LICENSE-cybench`. The challenges themselves are from Hack The Box's
Cyber Apocalypse 2024 CTF.

Each directory holds the flag, the official solution and a writeup, exactly as
upstream ships them. The harness builds and starts the target from here and
never mounts the directory into the agent's container. Reading the solution
yourself will not help you in the labs; the agent is the one being tested.

## Known upstream issue, left in place

`flag-command/docker-compose.yml` declares a healthcheck of
`nc -z localhost 1337`. The current `python:3.11-alpine` base ships a BusyBox
`nc` without `-z`, so Docker reports the container `unhealthy` while the
service answers normally. This is the mirror image of a forwarded port that
accepts a connection for a dead service: a health signal that does not measure
the thing you care about. The harness checks readiness by making a request to
the service from inside the task network, and does not trust the healthcheck.
