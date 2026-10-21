#!/usr/bin/env python3
"""Exit successfully when workspace OAuth M2M credentials are non-placeholder values."""

import sys
from pathlib import Path

import hcl2


def auth_configured(path: Path) -> bool:
    try:
        with path.open() as handle:
            config = hcl2.load(handle)
    except Exception:
        # Malformed HCL (and OS errors): treat as not configured. Never let
        # parser text (which can echo credential fragments) propagate.
        return False

    def configured(name: str) -> bool:
        value = str(config.get(name, "") or "").strip()
        normalized = value.lower().replace("-", "_").replace(" ", "_")
        placeholders = ("placeholder", "change_me", "changeme", "your_client_id",
                        "your_client_secret", "your_workspace_host")
        return bool(value) and "<" not in value and ">" not in value and not any(
            marker in normalized for marker in placeholders
        )

    return all(configured(name) for name in (
        "databricks_workspace_host", "databricks_client_id", "databricks_client_secret"
    ))


if __name__ == "__main__":
    raise SystemExit(0 if len(sys.argv) == 2 and auth_configured(Path(sys.argv[1])) else 1)
