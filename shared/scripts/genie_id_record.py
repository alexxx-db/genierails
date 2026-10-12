#!/usr/bin/env python3
"""Terraform external-data program: read one Genie agent ID file back into state.

modules/workspace reads each create-path agent's ID file with this after its
create resource (terraform_data.genie_space) ran, so the workspace state records
which agent ID that create left (data.external.genie_space_id). Release checks
that the ID file still holds that ID before it pushes config to the agent
(scripts/agent_ownership.py verify_created). Reads only; a missing file reads as "".

stdin:  {"id_file": "<path>"}
stdout: {"id": "<agent ID or empty>", "id_file": "<resolved path>"}
"""

import json
import sys
from pathlib import Path


def main() -> int:
    query = json.load(sys.stdin)
    path = Path(str(query.get("id_file") or "")).resolve()
    space_id = path.read_text().strip() if path.is_file() else ""
    json.dump({"id": space_id, "id_file": str(path)}, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
