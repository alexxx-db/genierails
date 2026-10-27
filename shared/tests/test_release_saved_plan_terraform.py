"""Real Terraform: a release applies only the saved plan its manifest names (rollout step 9).

The runner (scripts/terraform_layer.sh) plans with -out and applies with
``apply-plan``; release_gate.py records each root's state serial and plan
hash in the manifest and refuses when either moved. These tests run real
Terraform on a root with only the built-in terraform_data resource (no
provider download), through the real runner, in a copy of shared/.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import release_gate as rg
from tests.terraform_helpers import TIMEOUT, shared_copy

pytestmark = pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform not installed")

ROOT_TF = """
terraform {
  backend "local" {}
}

variable "value" {
  type    = string
  default = "v1"
}

# The runner always sets it (empty outside a pipeline job).
variable "databricks_pipeline_auth_type" {
  type    = string
  default = ""
}

resource "terraform_data" "governed" {
  input = var.value
}
"""


@pytest.fixture
def layer(tmp_path):
    shared = shared_copy(tmp_path)
    root = shared / "roots" / "account"
    for path in root.glob("*.tf"):
        path.unlink()
    (root / "main.tf").write_text(ROOT_TF)
    env_dir = tmp_path / "envs" / "account"
    env_dir.mkdir(parents=True)
    # Every env has var files (auth.auto.tfvars at least); this one sets nothing.
    (env_dir / "env.auto.tfvars").write_text("# no settings\n")
    runner = shared / "scripts" / "terraform_layer.sh"

    def run(*args, value="v1"):
        environ = os.environ | {"LAYER_ENV_DIR": str(env_dir), "TF_VAR_value": value, "TF_IN_AUTOMATION": "1"}
        result = subprocess.run([str(runner), "account", "account", *args], text=True, capture_output=True,
                                env=environ, timeout=TIMEOUT)
        return result

    return env_dir, run


def _serial(env_dir: Path):
    state = json.loads((env_dir / "terraform.tfstate").read_text())
    return {"serial": state["serial"], "lineage": state["lineage"]}


def test_a_saved_plan_applies_exactly_and_its_hash_is_stable(layer, tmp_path):
    env_dir, run = layer
    assert run("apply", "-auto-approve", "-input=false").returncode == 0
    plan = tmp_path / "release.tfplan"
    result = run("plan", "-input=false", "-detailed-exitcode", f"-out={plan}", value="v2")
    assert result.returncode == 2, result.stdout + result.stderr  # 2: the plan changes something
    digest = rg.sha256_file(plan)
    before = _serial(env_dir)
    result = run("apply-plan", str(plan))
    assert result.returncode == 0, result.stdout + result.stderr
    assert rg.sha256_file(plan) == digest
    assert _serial(env_dir)["serial"] > before["serial"]
    state = json.loads((env_dir / "terraform.tfstate").read_text())
    assert state["resources"][0]["instances"][0]["attributes"]["input"]["value"] == "v2"


def test_a_state_change_after_the_plan_is_caught_by_the_manifest_and_by_terraform(layer, tmp_path):
    env_dir, run = layer
    assert run("apply", "-auto-approve", "-input=false").returncode == 0
    plan = tmp_path / "release.tfplan"
    assert run("plan", "-input=false", f"-out={plan}", value="v2").returncode == 0
    recorded = _serial(env_dir)
    # Someone applies outside the manifest between release-plan and release.
    assert run("apply", "-auto-approve", "-input=false", value="v3").returncode == 0
    assert _serial(env_dir) != recorded  # release_gate.manifest_check refuses on this
    result = run("apply-plan", str(plan))
    assert result.returncode != 0
    assert "Saved plan is stale" in result.stdout + result.stderr
    state = json.loads((env_dir / "terraform.tfstate").read_text())
    assert state["resources"][0]["instances"][0]["attributes"]["input"]["value"] == "v3"


def test_state_serials_reads_each_root(tmp_path, layer):
    env_dir, run = layer
    assert run("apply", "-auto-approve", "-input=false").returncode == 0
    prod = tmp_path / "envs" / "prod"
    (prod / "data_access").mkdir(parents=True)
    serials = rg.state_serials(prod)
    assert serials["account"] == _serial(env_dir)
    assert serials["data_access"] is None and serials["workspace"] is None
