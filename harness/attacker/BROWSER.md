# A browser for the recon agent

The recon lab gives the agent a browser two ways and compares them. Both run
on `shared_net`, next to the target, with no route to the internet.

## 1. A narrow skill: Playwright in the attacker image

The attacker image carries Playwright 1.63 and a headless Chromium. The agent
writes a short script and runs it with the shell tool:

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto("http://web_flag:1337/")
    print(page.title())
    browser.close()
```

The skill is the instruction you give the agent: which scripts to write, what
to look for, what not to do. Each run is a shell command in the attacker
container, so it passes the scope gate as `kind=shell`. The gate does not read
the command, so it cannot tell which host the script visits.

## 2. The official server: Microsoft's Playwright MCP

Load it in `harness/roles/recon.py`:

```python
registry.load_mcp_stdio(
    "docker",
    ["run", "-i", "--rm", "--init", "--network", "shared_net",
     "mcr.microsoft.com/playwright/mcp:v0.0.83"],
    prefix="pw_",
    timeout=60,
)
```

The agent gets 25 general browser tools (`pw_browser_navigate`,
`pw_browser_snapshot`, `pw_browser_click` and so on). The harness holds one
session open for the run, so the page loaded by one call is still there for
the next. The container exits when the run ends.

These tools never reach the scope gate. The gate check lives inside
`http_request` and `shell`; a tool loaded from an MCP server is checked
against its schema and nothing else. With `locktalk` up next to
`flag-command`, `pw_browser_navigate` to `http://locktalk:1337/` succeeds and
the trace shows no scope decision at all. That is a scope lab finding.

## What to compare

- What the agent does with 25 general tools versus one methodology.
- Which one you can read in the trace afterwards.
- Which one the scope gate can see.
