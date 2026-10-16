#!/usr/bin/env python3
"""Fail fast when env.auto.tfvars still holds an active ``<...>`` placeholder.

The dev-to-prod template ships ``genie_space_id = "<your-genie-space-id>"``.
Real Genie agent IDs never contain angle brackets, so any ``<...>`` value is an
unfilled placeholder that would otherwise surface later as a confusing API error.

Usage:
    python genie_space_placeholder.py <path/to/env.auto.tfvars> [<more env files>...]
"""

import os
import sys
from pathlib import Path

PLACEHOLDER = "<your-genie-space-id>"


def _display_path(path: Path) -> str:
    resolved = Path(path).resolve()
    if resolved.parent.parent.name == "envs":
        return f"envs/{resolved.parent.name}/{resolved.name}"
    return str(path)


def placeholder_genie_space_ids(config: dict) -> list[str]:
    """Return every genie_spaces[].genie_space_id that is still a placeholder."""
    found = []
    for space in config.get("genie_spaces") or []:
        if not isinstance(space, dict):
            continue
        space_id = str(space.get("genie_space_id") or "")
        if "<" in space_id or ">" in space_id:
            found.append(space_id)
    return found


def placeholder_error(config: dict, path: Path, *, allow_access_tier_placeholders: bool = False) -> str | None:
    """Return the first actionable placeholder error, else None."""
    found = placeholder_genie_space_ids(config)
    if found:
        return (
            f"replace placeholder {found[0]} in {_display_path(path)} with your Genie agent ID "
            "(Genie UI > your agent > Configure > About this agent > Agent ID, "
            "or the <id> in its /genie/rooms/<id> URL)"
        )
    deployer = config.get("deployer_principal_id")
    if isinstance(deployer, str) and deployer.startswith("<") and deployer.endswith(">"):
        return (f"replace placeholder {deployer} in {_display_path(path)} with the application ID of the "
                "service principal that applies this env")
    groups = config.get("access_tier_groups")
    if isinstance(groups, list) and not allow_access_tier_placeholders:
        placeholder = next((group for group in groups if isinstance(group, str)
                            and group.startswith("<") and group.endswith(">")), None)
        if placeholder:
            return (f"replace placeholder {placeholder} in {_display_path(path)} with an IdP-synced group "
                    "name; replace every access_tier_groups placeholder, most to least privileged")
    return None


def main() -> int:
    advice = "--advice" in sys.argv[1:]
    paths = [Path(arg) for arg in sys.argv[1:] if arg != "--advice"]
    # Legacy configs have no placeholders. Avoid importing the HCL parser in
    # bootstrap-only environments unless a placeholder can actually exist.
    if not any(path.exists() and ("<" in path.read_text() or ">" in path.read_text()) for path in paths):
        return 0
    import hcl2

    for path in paths:
        if not path.exists():
            continue
        with path.open() as handle:
            config = hcl2.load(handle)
        error = placeholder_error(
            config, path,
            allow_access_tier_placeholders=os.environ.get("GENIERAILS_ALLOW_ACCESS_TIER_PLACEHOLDERS") == "1",
        )
        if error:
            if advice:
                print(f"  Setup required: {error}")
            else:
                print(f"ERROR: {error}", file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
