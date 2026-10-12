#!/usr/bin/env python3
"""Move the scheduled governance job from the workspace state to data_access.

The job (databricks_job.scheduled_governance) used to be deployed from the
workspace root; it is now deployed from the data_access root, so governance-only
envs get it too. Terraform's moved blocks can't cross state files, so
terraform_layer.sh calls this before every command on either root: while the
workspace state of the env still holds the job and the data_access state
doesn't, ``terraform state mv`` moves it, unchanged. Both roots then plan no
change for the job: nothing is destroyed or recreated. Envs that never enabled
the job (and envs already moved) are left alone without running Terraform.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ADDRESS = "databricks_job.scheduled_governance"
STATE = "terraform.tfstate"


def holds_job(state: Path) -> bool:
    try:
        resources = json.loads(state.read_text()).get("resources") or []
    except (OSError, ValueError):
        return False
    return any(r.get("mode") == "managed" and r.get("type") == "databricks_job"
               and r.get("name") == "scheduled_governance" and r.get("instances")
               for r in resources)


def move(workspace_dir: Path, data_access_dir: Path) -> int:
    source, target = workspace_dir / STATE, data_access_dir / STATE
    if not holds_job(source):
        return 0
    if holds_job(target):
        print(f"ERROR: both {source} and {target} hold {ADDRESS}. Keep the data_access one: "
              f"check that both have the same job ID (terraform state show), then remove the "
              f"workspace copy with terraform state rm (it forgets, never destroys).", file=sys.stderr)
        return 1
    # An empty cwd: no backend config, so -state/-state-out name the files.
    with tempfile.TemporaryDirectory() as cwd:
        result = subprocess.run(
            ["terraform", "state", "mv", f"-state={source}", f"-state-out={target}",
             f"-backup={source}.backup", f"-backup-out={target}.backup", ADDRESS, ADDRESS],
            cwd=cwd, text=True, capture_output=True)
    if result.returncode != 0:
        print(f"ERROR: could not move {ADDRESS} from {source} to {target}:\n"
              f"{result.stdout}{result.stderr}", file=sys.stderr)
        return 1
    print(f"+ moved {ADDRESS} from the workspace state to the data_access state "
          f"(the job is unchanged; it is now deployed from the data_access root)", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace-dir", required=True, type=Path)
    parser.add_argument("--data-access-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    return move(args.workspace_dir, args.data_access_dir)


if __name__ == "__main__":
    raise SystemExit(main())
