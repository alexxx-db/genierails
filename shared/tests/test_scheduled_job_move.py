"""The scheduled governance job moves from the workspace root to data_access.

Rollout step 15 (design 18.2): the job is deployed from the data_access root,
so governance-only envs get it. An env that already runs the job must never
see it destroyed or recreated:

- Terraform test (mock providers): the job as the old workspace root applied
  it, then the current workspace root (its removed block) and the current
  data_access root plan against that very state. The workspace plan forgets
  the job and destroys nothing; the data_access plan, holding the moved
  state, changes nothing. An env that never enabled the job (every legacy env
  by default) plans no change in either root.
- terraform_layer.sh moves the job between the env's real state files with
  terraform state mv before any command on either root, once, idempotently.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.terraform_helpers import tf, tf_env, tf_init

ROOT = Path(__file__).parents[2]
SHARED = ROOT / "shared"
RUNNER = SHARED / "scripts" / "terraform_layer.sh"
# origin/main before the move: the job in the workspace root.
PRE_MOVE_COMMIT = "7042a76"
ADDRESS = "databricks_job.scheduled_governance"
TERRAFORM = shutil.which("terraform")

pytestmark = pytest.mark.skipif(TERRAFORM is None, reason="terraform not installed")

SHIM = """
terraform {
  required_providers {
    databricks = {
      source                = "databricks/databricks"
      configuration_aliases = [databricks.workspace]
    }
  }
}
variable "databricks_workspace_host" {
  type    = string
  default = "https://example.invalid"
}
"""

ENABLED = """
  variables {
    enable_scheduled_governance        = true
    scheduled_governance_git_url       = "https://github.com/example/repo"
    scheduled_governance_config_source = "/Volumes/main/governance/genierails/envs"
    scheduled_governance_notification_emails = ["governance@example.com"]
  }
"""

TEST = """
mock_provider "databricks" {
  alias = "workspace"
}

run "old_workspace_root_applies_the_job" {
  state_key = "enabled"
  module {
    source = "./old_workspace"
  }
  providers = {
    databricks.workspace = databricks.workspace
  }
ENABLED
  assert {
    condition     = length(databricks_job.scheduled_governance) == 1
    error_message = "the old workspace root must have created the job"
  }
}

run "new_workspace_root_forgets_never_destroys" {
  state_key = "enabled"
  command   = plan
  module {
    source = "./new_workspace"
  }
  providers = {
    databricks.workspace = databricks.workspace
  }
ENABLED
}

run "new_data_access_root_with_the_moved_state_changes_nothing" {
  state_key = "enabled"
  command   = plan
  module {
    source = "./new_data_access"
  }
  providers = {
    databricks.workspace = databricks.workspace
  }
ENABLED
  assert {
    condition     = length(databricks_job.scheduled_governance) == 1
    error_message = "the data_access root must keep the moved job"
  }
}

run "legacy_env_old_workspace_root" {
  state_key = "disabled"
  module {
    source = "./old_workspace"
  }
  providers = {
    databricks.workspace = databricks.workspace
  }
}

run "legacy_env_new_workspace_root" {
  state_key = "disabled"
  command   = plan
  module {
    source = "./new_workspace"
  }
  providers = {
    databricks.workspace = databricks.workspace
  }
}

run "legacy_env_new_data_access_root" {
  state_key = "disabled_data_access"
  command   = plan
  module {
    source = "./new_data_access"
  }
  providers = {
    databricks.workspace = databricks.workspace
  }
}
""".replace("ENABLED\n", ENABLED)

CHANGE = re.compile(r"# (\S+) (will be (?:created|destroyed|updated in-place)|must be replaced"
                    r"|will no longer be managed by Terraform)")


def _sections(output: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current = None
    for line in output.splitlines():
        match = re.match(r'\s*run "([^"]+)"\.\.\. (\w+)', line)
        if match:
            current = match.group(1)
            sections[current] = match.group(2) + "\n"
        elif current:
            sections[current] += line + "\n"
    return sections


def _old_workspace_file() -> str:
    shown = subprocess.run(["git", "show", f"{PRE_MOVE_COMMIT}:shared/roots/workspace/scheduled_governance.tf"],
                           cwd=ROOT, text=True, capture_output=True)
    if shown.returncode != 0:
        message = f"pre-move commit {PRE_MOVE_COMMIT} is not in this clone (shallow checkout?)"
        if os.environ.get("REQUIRE_TERRAFORM_TESTS") == "1":
            pytest.fail(message)
        pytest.skip(message)
    assert 'resource "databricks_job" "scheduled_governance"' in shown.stdout
    return shown.stdout


def test_existing_job_is_never_destroyed_or_recreated(tmp_path):
    harness = tmp_path / "harness"
    sources = {
        "old_workspace": _old_workspace_file(),
        "new_workspace": (SHARED / "roots/workspace/scheduled_governance.tf").read_text(),
        "new_data_access": (SHARED / "roots/data_access/scheduled_governance.tf").read_text(),
    }
    for name, text in sources.items():
        (harness / name).mkdir(parents=True)
        (harness / name / "scheduled_governance.tf").write_text(text)
        (harness / name / "shim.tf").write_text(SHIM)
    (harness / "main.tf").write_text(SHIM)
    (harness / "tests").mkdir()
    (harness / "tests/move.tftest.hcl").write_text(TEST)
    env = tf_env(tmp_path)
    tf_init(harness, env=env)
    result = tf(harness, "test", "-no-color", "-verbose", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    runs = _sections(result.stdout)
    assert len(runs) == 6 and all(s.startswith("pass") for s in runs.values()), result.stdout

    forget = runs["new_workspace_root_forgets_never_destroys"]
    assert set(CHANGE.findall(forget)) == {
        (f"{ADDRESS}[0]", "will no longer be managed by Terraform")}, forget
    assert "0 to destroy" in forget and "will be destroyed" not in forget, forget

    moved = runs["new_data_access_root_with_the_moved_state_changes_nothing"]
    assert CHANGE.findall(moved) == [], moved
    assert "to add" not in moved and "to destroy" not in moved, moved

    for name in ("legacy_env_new_workspace_root", "legacy_env_new_data_access_root"):
        assert CHANGE.findall(runs[name]) == [], runs[name]
        assert "No changes." in runs[name], runs[name]


# ── terraform_layer.sh moves the job between the env's state files ───────────

def _state(lineage: str, serial: int, *resources: dict) -> dict:
    return {"version": 4, "terraform_version": "1.11.4", "serial": serial, "lineage": lineage,
            "outputs": {}, "resources": list(resources), "check_results": None}


JOB = {"mode": "managed", "type": "databricks_job", "name": "scheduled_governance",
       "provider": 'provider["registry.terraform.io/databricks/databricks"].workspace',
       "instances": [{"index_key": 0, "schema_version": 2, "sensitive_attributes": [],
                      "attributes": {"id": "123456", "name": "GenieRails steady-state governance (prod)"}}]}
OTHER = {"mode": "managed", "type": "null_resource", "name": "genie_space_acls",
         "provider": 'provider["registry.terraform.io/hashicorp/null"]',
         "instances": [{"schema_version": 0, "sensitive_attributes": [], "attributes": {"id": "1", "triggers": None}}]}
GRANT = {**OTHER, "name": "grants"}


def _env_dirs(tmp_path):
    ws = tmp_path / "envs/prod"
    da = ws / "data_access"
    da.mkdir(parents=True)
    return ws, da


def _run_layer(tmp_path, layer, env_dir, *command):
    """terraform_layer.sh with a terraform that only logs, except state mv,
    which runs the real terraform (so the move is Terraform's own)."""
    log = tmp_path / "terraform.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "terraform"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\n'
                    f'if [ "$1" = state ] && [ "$2" = mv ]; then exec "{TERRAFORM}" "$@"; fi\n')
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "LAYER_ENV_DIR": str(env_dir)}
    result = subprocess.run([RUNNER, layer, "prod", *(command or ("state-list",))], text=True, capture_output=True, env=env)
    return result, (log.read_text() if log.exists() else "")


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


@pytest.mark.parametrize("layer", ["workspace", "data_access"])
def test_layer_runner_moves_the_job_once_from_either_root(tmp_path, layer):
    ws, da = _env_dirs(tmp_path)
    (ws / "terraform.tfstate").write_text(json.dumps(_state("ws", 7, JOB, OTHER)))
    (da / "terraform.tfstate").write_text(json.dumps(_state("da", 3, GRANT)))

    result, log = _run_layer(tmp_path, layer, ws if layer == "workspace" else da)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"+ moved {ADDRESS} from the workspace state to the data_access state" in result.stderr
    workspace, data_access = _load(ws / "terraform.tfstate"), _load(da / "terraform.tfstate")
    assert [r["name"] for r in workspace["resources"]] == ["genie_space_acls"]
    assert workspace["lineage"] == "ws" and data_access["lineage"] == "da"
    moved = [r for r in data_access["resources"] if r["type"] == "databricks_job"]
    assert moved == [JOB], "the job must move unchanged (same ID, provider and attributes)"
    assert (ws / "terraform.tfstate.backup").exists() and (da / "terraform.tfstate.backup").exists()
    assert not list(ws.glob("terraform.tfstate.*[0-9].backup")), "backups keep the ignored standard name"
    assert log.splitlines()[-1] == "state list", log

    # Idempotent: the next command on either root runs no state mv at all.
    before = (ws / "terraform.tfstate").read_text(), (da / "terraform.tfstate").read_text()
    for again_layer, env_dir in (("workspace", ws), ("data_access", da)):
        again, log = _run_layer(tmp_path, again_layer, env_dir)
        assert again.returncode == 0 and "moved" not in again.stderr, again.stderr
    assert "state mv" not in log.split("state list")[-1]
    assert ((ws / "terraform.tfstate").read_text(), (da / "terraform.tfstate").read_text()) == before


def test_layer_runner_leaves_envs_without_the_job_alone(tmp_path):
    ws, da = _env_dirs(tmp_path)
    (ws / "terraform.tfstate").write_text(json.dumps(_state("ws", 7, OTHER)))
    for layer, env_dir in (("workspace", ws), ("data_access", da)):
        result, log = _run_layer(tmp_path, layer, env_dir)
        assert result.returncode == 0, result.stderr
    assert "state mv" not in log
    assert not (da / "terraform.tfstate").exists()
    # print-cmd never touches state.
    (ws / "terraform.tfstate").write_text(json.dumps(_state("ws", 7, JOB)))
    result, log = _run_layer(tmp_path, "workspace", ws, "print-cmd", "plan")
    assert result.returncode == 0 and "state mv" not in log
    assert _load(ws / "terraform.tfstate")["resources"] == [JOB]


def test_layer_runner_refuses_when_both_states_hold_the_job(tmp_path):
    ws, da = _env_dirs(tmp_path)
    (ws / "terraform.tfstate").write_text(json.dumps(_state("ws", 7, JOB)))
    (da / "terraform.tfstate").write_text(json.dumps(_state("da", 3, JOB)))
    before = (ws / "terraform.tfstate").read_text(), (da / "terraform.tfstate").read_text()
    result, log = _run_layer(tmp_path, "data_access", da, "plan")
    assert result.returncode != 0
    assert "both" in result.stderr and "terraform state rm" in result.stderr
    assert "plan" not in log.replace("state mv", "")
    assert ((ws / "terraform.tfstate").read_text(), (da / "terraform.tfstate").read_text()) == before
