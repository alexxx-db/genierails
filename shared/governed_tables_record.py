"""Model-free parsing of the committed governed-table record."""

from __future__ import annotations

import json
from pathlib import Path


MANIFEST = Path("generated/governed_tables.json")


def is_table(name: str) -> bool:
    parts = name.split(".")
    return len(parts) == 3 and all(parts) and not any(c in name for c in "*?[]")


def committed_agent_tables(env_dir: Path) -> dict[str, list[str]]:
    """The valid agent_tables entries the record holds now ({} when absent)."""
    try:
        raw = json.loads((Path(env_dir) / MANIFEST).read_text())
    except (OSError, ValueError):
        return {}
    value = raw.get("agent_tables") if isinstance(raw, dict) else None
    if not isinstance(value, dict):
        return {}
    return {
        key: list(tables) for key, tables in value.items()
        if isinstance(tables, list) and all(isinstance(t, str) and is_table(t) for t in tables)
    }
