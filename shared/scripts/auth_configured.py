#!/usr/bin/env python3
"""Exit successfully when workspace OAuth M2M credentials are non-placeholder values."""

import sys
from enum import Enum
from pathlib import Path

import hcl2


class AuthStatus(Enum):
    CONFIGURED = "configured"
    INCOMPLETE = "incomplete"
    MALFORMED = "malformed"


def auth_status(path: Path) -> AuthStatus:
    try:
        with path.open() as handle:
            config = hcl2.load(handle)
    except FileNotFoundError:
        return AuthStatus.INCOMPLETE
    except Exception:
        # Never let parser text (which can echo credential fragments) propagate.
        return AuthStatus.MALFORMED

    def configured(name: str) -> bool:
        value = str(config.get(name, "") or "").strip()
        normalized = value.lower().replace("-", "_").replace(" ", "_")
        placeholders = ("placeholder", "change_me", "changeme", "your_client_id",
                        "your_client_secret", "your_workspace_host")
        return bool(value) and "<" not in value and ">" not in value and not any(
            marker in normalized for marker in placeholders
        )

    return AuthStatus.CONFIGURED if all(configured(name) for name in (
        "databricks_workspace_host", "databricks_client_id", "databricks_client_secret"
    )) else AuthStatus.INCOMPLETE


def auth_configured(path: Path) -> bool:
    """Compatibility predicate for callers that only need yes/no."""
    return auth_status(path) is AuthStatus.CONFIGURED


if __name__ == "__main__":
    status = auth_status(Path(sys.argv[1])) if len(sys.argv) == 2 else AuthStatus.INCOMPLETE
    raise SystemExit({AuthStatus.CONFIGURED: 0, AuthStatus.INCOMPLETE: 1, AuthStatus.MALFORMED: 2}[status])
