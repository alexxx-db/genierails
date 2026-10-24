#!/usr/bin/env python3
"""Live Genie round-trip fidelity check (design section 14). Never run by default.

    DATABRICKS_LIVE_TESTS=1 python3 genie_roundtrip_live.py --space-id <agent> [--warehouse-id <id>]

Exports the agent's serialized_space exactly as make capture does, creates a
scratch agent from it, exports the scratch agent, and fails unless the two
exports are identical (field-level diff printed). The scratch agent is
deleted in every case. Credentials come only from the selected environment's
auth.auto.tfvars.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

SHARED = Path(__file__).resolve().parents[1]
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from scripts.agent_ownership import AgentError, SPACE_API, changed_fields, payload_from_response, render  # noqa: E402


def _export(api, space_id: str, attempts: int = 6) -> dict:
    for attempt in range(attempts):
        try:
            return payload_from_response(api.do("GET", SPACE_API.format(space_id),
                                                query={"include_serialized_space": "true"}))
        except AgentError:
            if attempt == attempts - 1:
                raise
            time.sleep(10)  # a new agent can take a moment before it serializes
    raise AssertionError("unreachable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--space-id", required=True)
    parser.add_argument("--warehouse-id", default="", help="warehouse for the scratch agent (default: the source's)")
    parser.add_argument("--auth-file", type=Path, default=Path("auth.auto.tfvars"))
    args = parser.parse_args(argv)
    if os.environ.get("DATABRICKS_LIVE_TESTS") != "1":
        print("genie_roundtrip_live: live check; set DATABRICKS_LIVE_TESTS=1 to run it.", file=sys.stderr)
        return 2

    from scripts.auth_configured import workspace_client
    api = workspace_client(args.auth_file).api_client
    source = api.do("GET", SPACE_API.format(args.space_id), query={"include_serialized_space": "true"})
    exported = payload_from_response(source)
    warehouse = args.warehouse_id or source.get("warehouse_id") or ""
    if not warehouse:
        print("genie_roundtrip_live: the source agent has no warehouse_id; pass --warehouse-id", file=sys.stderr)
        return 1
    scratch_id = ""
    try:
        created = api.do("POST", "/api/2.0/genie/spaces", body={
            "title": f"genierails round-trip scratch {int(time.time())}",
            "warehouse_id": warehouse,
            "serialized_space": json.dumps(exported, separators=(",", ":")),
        })
        scratch_id = str(created.get("space_id") or "")
        if not scratch_id:
            print(f"genie_roundtrip_live: create returned no space_id: {created}", file=sys.stderr)
            return 1
        again = _export(api, scratch_id)
        diff = changed_fields(exported, again)
        if diff or render(again) != render(exported):
            print(f"genie_roundtrip_live: FAIL, {len(diff)} field(s) changed in the round trip:", file=sys.stderr)
            for field in diff:
                print(f"  {field}", file=sys.stderr)
            return 1
        print(f"genie_roundtrip_live: PASS ({args.space_id} -> scratch {scratch_id}: identical export)")
        return 0
    finally:
        if scratch_id:
            api.do("DELETE", SPACE_API.format(scratch_id))
            print(f"genie_roundtrip_live: deleted scratch agent {scratch_id}")


if __name__ == "__main__":
    raise SystemExit(main())
