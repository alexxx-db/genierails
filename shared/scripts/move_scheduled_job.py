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
State is found through scripts/state_layout.py (in the checkout or under
GENIERAILS_STATE_DIR). A remote-state env still holding the job refuses
with the commands to move it: -state/-state-out work on local files only.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ADDRESS = "databricks_job.scheduled_governance"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state_layout  # noqa: E402


def holds_job(state: dict | None) -> bool:
    resources = (state or {}).get("resources") or []
    return any(r.get("mode") == "managed" and r.get("type") == "databricks_job"
               and r.get("name") == "scheduled_governance" and r.get("instances")
               for r in resources)


def _state(env_dir: Path, layer: str) -> dict | None:
    return state_layout.state_json(env_dir, env_dir.name, layer)


def move(workspace_dir: Path, data_access_dir: Path) -> int:
    """0 when nothing needs moving or the job moved; 1 otherwise, including any unreadable state."""
    try:
        return _move(Path(workspace_dir))
    except RuntimeError as exc:  # never "no job": a state that can't be read stops the command
        print(f"ERROR: cannot check {ADDRESS} before running Terraform: {exc}", file=sys.stderr)
        return 1


def _move(workspace_dir: Path) -> int:
    if state_layout.remote_configured(workspace_dir):
        if not holds_job(_state(workspace_dir, "workspace")):
            return 0
        print(f"ERROR: the remote workspace state of {workspace_dir.name} still holds {ADDRESS}. Move it to "
              f"the data_access state by hand: terraform state pull both, terraform state mv "
              f"-state=<workspace> -state-out=<data_access> {ADDRESS} {ADDRESS}, then terraform state push "
              f"both (data_access first).", file=sys.stderr)
        return 1
    paths = state_layout.root_state_paths(workspace_dir)
    source, target = paths["workspace"], paths["data_access"]
    if not holds_job(_state(workspace_dir, "workspace")):
        return 0
    if holds_job(_state(workspace_dir, "data_access")):
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
