# mcp-skeleton

A small MCP server for the recon lab. It gives the recon agent a folder of
notes to search: your documentation, threat models or rules of engagement.
The transport, the schema and one tool are done. You write the second tool.

| File | What it is |
|---|---|
| `server.py` | The server. `search_notes` is finished; `read_note` is a `TODO(M2)` |
| `notes/` | Three sample notes. Point `NOTES_DIR` at your own folder instead |

## Run it on its own

```bash
python mcp-skeleton/server.py
```

It speaks MCP over stdin and stdout, so it starts and then waits. Ctrl-C to
stop. If it exits with an import error, run
`pip install -r harness/requirements.txt` in your venv.

## Give it to the recon agent

In `harness/roles/recon.py`, where the `TODO(M2)` says to add your own tool:

```python
registry.load_mcp_stdio("python", ["mcp-skeleton/server.py"], prefix="mcp_")
```

Run the harness from the repository root with your venv active, so `python`
is the venv's Python and the relative path resolves. The tools arrive as
`mcp_search_notes` and `mcp_read_note`, with the schemas the server declares.
The harness validates every call against those schemas before it leaves the
process, exactly as it does for its own tools.

Each tool's docstring is the description the model reads. Write it for the
model: what the tool does, when to reach for it, what it returns. Rules about
how the agent should behave belong in the role's system prompt, not here.

## What the scope gate does not see

MCP tools do not pass through the scope gate. The gate check lives inside the
harness's own `http_request` and `shell` tools; `registry.call` only validates
the input schema. A notes server never touches the target, so that is fine
here. An MCP server that can reach a network is a different matter, and it
comes back in the scope lab.

The loader starts the server once, when its tools are registered, and keeps
that session for the whole run, so a server that remembers something between
calls (a browser, say) keeps it. The server shuts down when the run ends.
