#!/usr/bin/env python3
"""Validate environment-owned deterministic-governance configuration."""
import sys
import os
from pathlib import Path

import hcl2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_files import AgentFileError, merged_config  # noqa: E402
from deterministic_governance import validate_config  # noqa: E402


def main() -> int:
    path = Path(sys.argv[1])
    try:
        cfg = hcl2.loads(path.read_text())
        # The per-agent files (agents/*.auto.tfvars) are validated with it.
        cfg = merged_config(path.parent, cfg)
    except AgentFileError as exc:
        print(f"env config validation: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"env config validation: {path}: {exc}", file=sys.stderr)
        return 1
    errors = validate_config(
        cfg,
        ack_unclassified=os.environ.get("ACK_UNCLASSIFIED"),
        ack_weaken=os.environ.get("ACK_WEAKEN"),
    )
    if errors:
        for error in errors:
            print(f"env config validation: {error}", file=sys.stderr)
        return 1
    print("env config validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
