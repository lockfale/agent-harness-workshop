"""M2: your own MCP server. The plumbing is done; one tool is yours to write.

This server hands the recon agent a folder of notes: your documentation,
threat models, design notes, rules of engagement. That is the "docs" option
from the recon lab. Wrapping a tool you already run, or an API, is the same
shape: a thin layer that validates input, does one job, and returns text.

Two tools:

* `search_notes` is finished. Read it before you write the other one.
* `read_note` is the exercise. It is also the place where "what you
  deliberately do not expose" stops being abstract: a note name is a path, and
  a path can point anywhere on your laptop.

Each tool's docstring is its description. The model reads it on every turn to
decide whether and how to call the tool, so it is written for the model, not
for you. Comments (`#`) are for you; the model never sees them. Deciding what
goes in the description and what goes in the role's system prompt is where
most homemade MCP servers go wrong.

Run it under the harness (see README.md), or on its own to check it starts:

    python mcp-skeleton/server.py

It speaks MCP over stdin and stdout, so on its own it sits and waits. Ctrl-C
to stop.
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Point NOTES_DIR at your own folder to serve your own notes.
NOTES_DIR = Path(
    os.environ.get("NOTES_DIR") or Path(__file__).resolve().parent / "notes"
).resolve()

# Caps keep one careless call from pouring a whole folder into the context
# window, where the model pays for every token on every later turn.
MAX_MATCHES = 20
MAX_LINE_CHARS = 200
NOTE_SUFFIXES = (".md", ".txt")

server = FastMCP("notes", log_level="WARNING")  # quiet: stderr is your terminal


def _notes() -> list[Path]:
    return sorted(
        p for p in NOTES_DIR.rglob("*") if p.is_file() and p.suffix in NOTE_SUFFIXES
    )


@server.tool()
def search_notes(term: str) -> str:
    """Search the operator's notes for a word or phrase, case-insensitive.

    Returns matching lines as `file:line: text`, at most 20. Use it to check
    the rules of engagement, methodology or prior findings before acting. The
    file names it prints are what read_note expects.
    """
    term = term.strip()
    if not term:
        # A clear error the model can act on beats an empty result it has to
        # interpret.
        return "Give a word or phrase to search for."

    needle = term.lower()
    matches: list[str] = []
    total = 0
    for path in _notes():
        rel = path.relative_to(NOTES_DIR).as_posix()
        for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if needle in line.lower():
                total += 1
                if len(matches) < MAX_MATCHES:
                    matches.append(f"{rel}:{number}: {line.strip()[:MAX_LINE_CHARS]}")

    if not matches:
        # Say so plainly. "Nothing found" is a result, and a model told
        # nothing tends to call again with the same term.
        return f"No note mentions {term!r}."
    if total > len(matches):
        matches.append(f"({total - len(matches)} more matches not shown; narrow the term)")
    return "\n".join(matches)


@server.tool()
def read_note(name: str) -> str:
    """Return the full text of one note, by the file name search_notes printed."""
    # TODO(M2): implement this.
    #
    # `name` comes from the model, and the model got it from text that may
    # have come from the target. Treat it as hostile:
    #
    #   * resolve it against NOTES_DIR and refuse anything that lands outside
    #     the folder (`../../.ssh/id_ed25519`, an absolute path, a symlink
    #     that points out). `Path.resolve()` and `Path.is_relative_to()` do
    #     most of the work;
    #   * refuse anything that is not a note (NOTE_SUFFIXES);
    #   * cap what you return, the way search_notes does.
    #
    # Return a short, plain refusal rather than raising: the model should
    # learn the boundary, not retry around a stack trace. Then ask yourself
    # what else in this folder you would not want an agent to read.
    raise NotImplementedError("TODO(M2): implement read_note")


if __name__ == "__main__":
    server.run()  # stdio
