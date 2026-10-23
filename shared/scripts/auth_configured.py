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
    UNREADABLE = "unreadable"


def auth_status(path: Path) -> AuthStatus:
    try:
        with path.open() as handle:
            config = hcl2.load(handle)
    except FileNotFoundError:
        return AuthStatus.INCOMPLETE
    except (OSError, UnicodeError):
        return AuthStatus.UNREADABLE
    except Exception:
        # Never let parser text (which can echo credential fragments) propagate.
        return AuthStatus.MALFORMED

    def configured(name: str) -> bool:
        raw = config.get(name, "")
        if not isinstance(raw, str):
            return False
        value = raw.strip()
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


def workspace_client(auth_file: Path, **kwargs):
    """Return an OAuth-M2M client pinned only to ``auth_file``."""
    path = Path(auth_file)
    status = auth_status(path)
    if status is AuthStatus.UNREADABLE:
        raise RuntimeError(f"{path} can't be read") from None
    if status is AuthStatus.MALFORMED:
        raise RuntimeError(f"{path} is not valid HCL") from None
    if status is AuthStatus.INCOMPLETE:
        raise RuntimeError(f"fill in {path} (workspace host, client ID, and client secret)") from None
    try:
        with path.open() as handle:
            config = hcl2.load(handle)
    except (OSError, UnicodeError):
        raise RuntimeError(f"{path} can't be read") from None
    except Exception:
        raise RuntimeError(f"{path} is not valid HCL") from None
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.config import Config
    sdk_config = Config(
        host=config["databricks_workspace_host"].strip(),
        client_id=config["databricks_client_id"].strip(),
        client_secret=config["databricks_client_secret"].strip(),
        auth_type="oauth-m2m",
        **kwargs,
    )
    return WorkspaceClient(config=sdk_config)


if __name__ == "__main__":
    status = auth_status(Path(sys.argv[1])) if len(sys.argv) == 2 else AuthStatus.INCOMPLETE
    raise SystemExit({AuthStatus.CONFIGURED: 0, AuthStatus.INCOMPLETE: 1,
                      AuthStatus.MALFORMED: 2, AuthStatus.UNREADABLE: 3}[status])
