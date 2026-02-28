# Future Feature: Obsidian CLI Integration

**Status:** Proposal — not yet implemented
**Added:** 2026-02-28
**Requires:** Obsidian v1.12+ with CLI enabled, Obsidian desktop running

---

## Overview

Obsidian v1.12 ships an official CLI (`obsidian`) that exposes full vault operations from the terminal. This could replace or augment several parts of the orchestrator that currently use raw file I/O.

## Relevant CLI Commands

| Command | What it does | Replaces in orchestrator |
|---|---|---|
| `obsidian search query="..." limit=10` | Full-text vault search with operators | `memory.py` TF-IDF keyword search |
| `obsidian read file="Note"` | Read note content | Raw `open()` in `queue_manager.py` |
| `obsidian create name="Note" content="..."` | Create note with optional template | Raw `open()` writes |
| `obsidian append file="Note" content="..."` | Append to note | `queue_manager.py` result/log appending |
| `obsidian backlinks file="Note"` | Find notes linking to a note | Wikilink resolution in `queue_manager.py` |
| `obsidian links file="Note"` | Outbound links from a note | — |
| `obsidian properties file="Note"` | Read/write YAML frontmatter | Manual YAML parsing in `memory.py` |
| `obsidian tasks` | Query tasks across vault | `usage_suggester.py` `_scan_vault_tasks()` |
| `obsidian orphans` | Find isolated notes | Useful for vault-gardener skill |
| `obsidian unresolved` | Find broken wikilinks | Useful for vault-gardener skill |
| `obsidian move file="A" to="B"` | Move note, auto-update links | `memory.py` archival |
| `obsidian daily:append content="..."` | Append to daily note | Potential task result logging |
| `obsidian sync:status` | Check Obsidian Sync state | Heartbeat health check |
| `obsidian files folder="path" ext="md"` | List vault files with filters | `glob`-based file scanning |
| `obsidian eval code="app.vault..."` | Execute JS in Obsidian context | Advanced vault queries |

## Benefits

- **Better search**: Obsidian's built-in search understands links, tags, properties natively — more accurate than our TF-IDF keyword matching
- **Link-aware operations**: `move` auto-updates all backlinks; `backlinks` gives proper graph traversal
- **Task queries**: `tasks` command integrates with Obsidian Tasks plugin, no manual regex parsing needed
- **Frontmatter handling**: `properties` command handles YAML edge cases Obsidian knows about
- **Future-proof**: As Obsidian adds features, the CLI exposes them automatically

## Trade-offs

- **External dependency**: Obsidian desktop must be running — breaks our "works headless" guarantee
- **Latency**: CLI subprocess calls are slower than direct file reads
- **Windows path handling**: May need extra quoting for paths with spaces
- **Not pure stdlib**: Adds a runtime dependency beyond Python + pyyaml

## Proposed Design: Optional Enhancement

Use the CLI when available, fall back to raw file I/O otherwise:

```python
# In a new module: obsidian_cli.py

import shutil
import subprocess
import json

def is_available() -> bool:
    """Check if obsidian CLI is installed and responsive."""
    return shutil.which("obsidian") is not None

def search(query: str, limit: int = 10) -> list[dict]:
    """Search vault via CLI, returns list of {file, line, snippet}."""
    result = subprocess.run(
        ["obsidian", "search", f'query="{query}"', f"limit={limit}"],
        capture_output=True, text=True, encoding="utf-8"
    )
    # Parse output...

def read_note(name: str) -> str:
    """Read note content via CLI."""
    result = subprocess.run(
        ["obsidian", "read", f'file="{name}"'],
        capture_output=True, text=True, encoding="utf-8"
    )
    return result.stdout
```

Integration points:
- `memory.py`: Use `obsidian search` instead of TF-IDF when CLI available
- `queue_manager.py`: Use `obsidian backlinks` for wikilink resolution
- `usage_suggester.py`: Use `obsidian tasks` instead of manual task scanning
- `heartbeat.py`: Add `obsidian sync:status` check handler
- `doctor.py`: Add CLI availability check

## Implementation Effort

- **Low risk**: Each integration point is independently useful, can be done incrementally
- **Fallback guaranteed**: Raw file I/O always works as fallback
- **Estimated work**: ~2-3 sessions for core integration (search + tasks + backlinks)

## References

- [Obsidian CLI — Official Docs](https://help.obsidian.md/cli)
- [Obsidian CLI Reference (DeepWiki)](https://deepwiki.com/victor-software-house/obsidian-help/7.1-obsidian-cli)
- [Obsidian Roadmap](https://obsidian.md/roadmap/)
