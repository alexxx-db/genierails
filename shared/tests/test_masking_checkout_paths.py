"""terraform_data.masking_functions and its drop don't depend on the checkout's paths.

The data_access state is shared by every checkout of the repo (a teammate's
clone, a CI runner, a moved repo, GENIERAILS_STATE_DIR). The live failure:
a plan from a second checkout replaced masking_functions only because its
triggers recorded absolute paths, and updated masking_functions_drop in place,
so make policy-abandon refused and a prod release hit its stop line.

The Terraform tests run the module's own masking blocks (cut from
modules/data_access/main.tf, with stand-ins for what they reference) in two
copies of a checkout that share one state file, with real Terraform. The
guard tests feed policy_generations the plan JSON Terraform made.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import policy_generations as pg
from tests.terraform_helpers import skip_if_providers_unavailable, tf, tf_env

SHARED = Path(__file__).resolve().parents[1]
MODULE = SHARED / "modules" / "data_access" / "main.tf"
# The pre-fix shape (paths in masking_functions' triggers and the drop's input).
PRE_FIX = SHARED / "modules" / "data_access" / "tests" / "fixtures" / "pre_stable_tags" / "main.tf"
SQL = "CREATE OR REPLACE FUNCTION cat.sch.mask_email(v STRING) RETURNS STRING RETURN '***';\n"
STUB = """import json, os, sys
with open(os.environ["MASKING_STUB_LOG"], "a") as log:
    log.write(json.dumps({"script": os.path.abspath(__file__), "argv": sys.argv[1:]}) + "\\n")
"""
# Stand-ins for what the cut blocks reference outside themselves.
SUPPORT = """
terraform {
  required_providers {
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
  }
}

variable "masking_sql_file" { type = string }
variable "deploy_masking_script" { type = string }
variable "auth_file" { type = string }
variable "masking_paths_base" {
  type    = string
  default = ""
}
variable "databricks_workspace_host" { type = string }
variable "databricks_client_id" { type = string }
variable "sql_warehouse_id" { type = string }

locals {
  effective_warehouse_id = var.sql_warehouse_id
}
"""
ROOT = """
terraform {
  backend "local" {}
}

variable "env_dir" { type = string }
variable "sql_warehouse_id" {
  type    = string
  default = "warehouse"
}

locals {
  project_root = abspath("${path.root}/../..")
}

module "data_access" {
  source                    = "../../modules/data_access"
  masking_sql_file          = "${var.env_dir}/masking_functions.sql"
  deploy_masking_script     = "${local.project_root}/deploy_masking_functions.py"
  auth_file                 = "${var.env_dir}/auth.auto.tfvars"
  masking_paths_base        = dirname(var.env_dir)
  databricks_workspace_host = "https://example.invalid"
  databricks_client_id      = "deployer"
  sql_warehouse_id          = var.sql_warehouse_id
}
"""
NO_PATHS_BASE = "  masking_paths_base        = dirname(var.env_dir)\n"

pytestmark = pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform not installed")


def _block(text: str, header: str) -> str:
    """The block opened by ``header`` (a regex), braces balanced, without depends_on."""
    match = re.search(header + r"\s*\{", text)
    assert match, header
    depth = 0
    for end in range(match.end() - 1, len(text)):
        depth += {"{": 1, "}": -1}.get(text[end], 0)
        if depth == 0:
            break
    block = text[match.start():end + 1]
    return re.sub(r"\n  depends_on = \[[^\]]*\]\n", "\n", block)


def masking_blocks(source: Path) -> str:
    text = source.read_text()
    blocks = [_block(text, r'data "external" "normalized_masking_sql"'),
              _block(text, r'resource "terraform_data" "masking_functions"'),
              _block(text, r'resource "terraform_data" "masking_functions_drop"')]
    if "masking_project_root" in text:  # the fix: relative paths for the drop
        blocks.insert(1, _block(text, r"locals(?=\s*\{\s*#[^\n]*\n(?:\s*#[^\n]*\n)*\s*masking_project_root)"))
    # The pre-fix fixture sits three levels down; its program path is fixed up.
    return "\n\n".join(blocks).replace("${path.module}/../../../normalize_masking_sql.py",
                                       "${path.module}/normalize_masking_sql.py")


def checkout(tmp_path: Path, name: str, module_source: Path = MODULE, *, root: str = ROOT) -> Path:
    """A checkout: shared/{deploy stub, SQL helpers, modules/data_access, roots/data_access, envs/dev}."""
    shared = tmp_path / name / "shared"
    module = shared / "modules" / "data_access"
    module.mkdir(parents=True)
    (module / "main.tf").write_text(SUPPORT + "\n" + masking_blocks(module_source) + "\n")
    shutil.copy(SHARED / "modules" / "data_access" / "normalize_masking_sql.py", module)
    for helper in ("sql_tokenizer.py", "masking_sql_blocks.py"):
        shutil.copy(SHARED / helper, shared / helper)
    (shared / "deploy_masking_functions.py").write_text(STUB)
    roots = shared / "roots" / "data_access"
    roots.mkdir(parents=True)
    (roots / "main.tf").write_text(root)
    env = shared / "envs" / "dev" / "data_access"
    env.mkdir(parents=True)
    (env / "masking_functions.sql").write_text(SQL)
    (env / "auth.auto.tfvars").write_text('databricks_workspace_host = "https://example.invalid"\n')
    return shared


class Layer:
    """terraform in one checkout's data_access root, on a state file every checkout shares."""

    def __init__(self, tmp_path: Path, shared: Path, state: Path, log: Path):
        self.root, self.state, self.env_dir = shared / "roots" / "data_access", state, shared / "envs" / "dev" / "data_access"
        self.env = tf_env(tmp_path / shared.parent.name, MASKING_STUB_LOG=str(log),
                          GENIERAILS_MASKING_BASE_DIR=str(self.env_dir.parent))
        # tf_init passes -backend=false; the shared local state needs the backend.
        skip_if_providers_unavailable(tf(self.root, "init", "-input=false", f"-backend-config=path={state}",
                                         env=self.env))

    def run(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return tf(self.root, *args, "-input=false", "-no-color", f"-var=env_dir={self.env_dir}", env=env or self.env)

    def apply(self) -> None:
        result = self.run("apply", "-auto-approve")
        assert result.returncode == 0, result.stdout + result.stderr

    def plan_json(self, tmp: Path) -> dict:
        plan = tmp / "plan.tfplan"
        result = self.run("plan", f"-out={plan}")
        assert result.returncode == 0, result.stdout + result.stderr
        shown = tf(self.root, "show", "-json", str(plan), env=self.env)
        assert shown.returncode == 0, shown.stderr
        return json.loads(shown.stdout)


def _changes(plan: dict) -> dict[str, list[str]]:
    return {c["address"]: c["change"]["actions"] for c in plan.get("resource_changes") or []
            if set(c["change"]["actions"]) - {"no-op", "read"}}


def _calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.is_file() else []


@pytest.fixture
def two_checkouts(tmp_path):
    state, log = tmp_path / "state" / "terraform.tfstate", tmp_path / "masking.log"
    state.parent.mkdir()
    a = Layer(tmp_path, checkout(tmp_path, "checkout-a"), state, log)
    b = Layer(tmp_path, checkout(tmp_path, "checkout-b"), state, log)
    return a, b, log


# -- the module ----------------------------------------------------------------------------
def test_a_plan_from_another_checkout_changes_nothing(two_checkouts, tmp_path):
    a, b, log = two_checkouts
    a.apply()
    assert [c["script"] for c in _calls(log)] == [str(a.root.parents[1] / "deploy_masking_functions.py")]
    plan = b.plan_json(tmp_path)
    assert _changes(plan) == {}, _changes(plan)


def test_a_plan_from_another_checkout_still_redeploys_a_changed_sql_or_warehouse(two_checkouts, tmp_path):
    a, b, _ = two_checkouts
    a.apply()
    (b.env_dir / "masking_functions.sql").write_text(SQL.replace("'***'", "'####'"))
    assert _changes(b.plan_json(tmp_path)) == {"module.data_access.terraform_data.masking_functions":
                                               ["delete", "create"]}
    (b.env_dir / "masking_functions.sql").write_text(SQL)
    result = b.run("plan", "-detailed-exitcode", "-var=sql_warehouse_id=other")
    assert result.returncode == 2 and "terraform_data.masking_functions must be replaced" in result.stdout


def test_a_destroy_from_another_checkout_drops_with_that_checkouts_files(two_checkouts):
    a, b, log = two_checkouts
    a.apply()
    shutil.rmtree(a.root.parents[2])  # checkout A is gone (a moved repo, a finished CI runner)
    log.unlink()
    result = b.run("destroy", "-auto-approve")
    assert result.returncode == 0, result.stdout + result.stderr
    [call] = _calls(log)
    argv = call["argv"]
    assert call["script"] == str(b.root.parents[1] / "deploy_masking_functions.py")
    assert "--drop" in argv
    base = argv[argv.index("--base-dir") + 1]
    assert base == str(b.env_dir.parent)
    for flag, name in (("--sql-file", "masking_functions.sql"), ("--auth-file", "auth.auto.tfvars")):
        recorded = argv[argv.index(flag) + 1]
        assert recorded == f"data_access/{name}"
        assert (Path(base) / recorded).is_file()


def test_a_destroy_without_the_base_dir_refuses_instead_of_guessing(two_checkouts, tmp_path):
    """Raw terraform destroy (not terraform_layer.sh): the real deployer refuses before it connects."""
    a, _, _ = two_checkouts
    a.apply()
    real = SHARED / "deploy_masking_functions.py"
    (a.root.parents[1] / "deploy_masking_functions.py").write_text(real.read_text())
    env = {k: v for k, v in a.env.items() if k != "GENIERAILS_MASKING_BASE_DIR"}
    result = a.run("destroy", "-auto-approve", env=env)
    assert result.returncode != 0
    assert "GENIERAILS_MASKING_BASE_DIR is unset" in result.stdout + result.stderr
    assert "nothing was dropped" in result.stdout + result.stderr


def test_the_runner_exports_the_base_dir_the_root_records_against():
    runner = (SHARED / "scripts" / "terraform_layer.sh").read_text()
    assert 'GENIERAILS_MASKING_BASE_DIR="$(dirname "$ENV_DIR")"' in runner
    assert "masking_paths_base" in (SHARED / "roots" / "data_access" / "main.tf").read_text()
    assert re.search(r"masking_paths_base\s*=\s*dirname\(var\.env_dir\)",
                     (SHARED / "roots" / "data_access" / "main.tf").read_text())


# -- the deployer's path resolution --------------------------------------------------------
def _resolve(*argv: str):
    sys.path.insert(0, str(SHARED))
    import deploy_masking_functions as dmf
    parsed = __import__("argparse").Namespace(**dict(zip(("sql_file", "auth_file", "base_dir"), argv)))
    dmf.resolve_paths(parsed)
    return parsed.sql_file, parsed.auth_file


def test_relative_paths_resolve_against_the_base_dir_and_absolute_ones_stay(tmp_path):
    assert _resolve("data_access/m.sql", "data_access/auth", str(tmp_path)) == (
        str(tmp_path / "data_access/m.sql"), str(tmp_path / "data_access/auth"))
    # State from before the fix records absolute paths: used as they are.
    assert _resolve("/old/m.sql", "/old/auth", str(tmp_path)) == ("/old/m.sql", "/old/auth")
    assert _resolve("m.sql", "auth", None) == ("m.sql", "auth")  # no --base-dir: as before
    with pytest.raises(SystemExit):
        _resolve("data_access/m.sql", "data_access/auth", "")


# -- the one-time re-key of existing state -------------------------------------------------
@pytest.fixture
def upgraded_plan(tmp_path):
    """State applied by the pre-fix module from checkout A, planned by the fixed one from checkout B."""
    state, log = tmp_path / "state" / "terraform.tfstate", tmp_path / "masking.log"
    state.parent.mkdir()
    old = Layer(tmp_path, checkout(tmp_path, "checkout-a", PRE_FIX, root=ROOT.replace(NO_PATHS_BASE, "")),
                state, log)
    old.apply()
    new = Layer(tmp_path, checkout(tmp_path, "checkout-b"), state, log)
    return new.plan_json(tmp_path), new


def test_the_upgrade_plans_only_a_path_only_rekey(upgraded_plan):
    plan, _ = upgraded_plan
    assert _changes(plan) == {
        "module.data_access.terraform_data.masking_functions": ["delete", "create"],
        "module.data_access.terraform_data.masking_functions_drop": ["update"],
    }
    assert all(pg.masking_rekey(c) for c in plan["resource_changes"] if c["address"] in _changes(plan))
    assert pg.rekey_notes(plan) == [pg.REKEY_NOTE]


def test_abandon_allows_the_rekey(upgraded_plan):
    plan, _ = upgraded_plan
    assert pg.plan_violations(plan, "abandon") == []
    assert pg.rebind_violations(plan, "rebind-protect", drops=lambda: False,
                                coverage={"recorded": [], "mapping": {}, "principals": set()}) == []


def _with(plan: dict, address_suffix: str, mutate) -> dict:
    plan = json.loads(json.dumps(plan))
    for change in plan["resource_changes"]:
        if change["address"].endswith(address_suffix):
            mutate(change["change"])
    return plan


@pytest.mark.parametrize("key", list(pg.DEPLOY_TRIGGERS))
def test_abandon_refuses_a_replace_that_changes_what_is_deployed(upgraded_plan, key):
    plan, _ = upgraded_plan
    changed = _with(plan, "terraform_data.masking_functions",
                    lambda c: c["after"]["triggers_replace"].__setitem__(key, "changed"))
    problems = pg.plan_violations(changed, "abandon")
    assert problems == ["module.data_access.terraform_data.masking_functions: delete/create "
                        "(abandon only deletes the unfinished change's policies)"]
    assert pg.rekey_notes(changed) == []


@pytest.mark.parametrize("key", list(pg.DROP_TARGET))
def test_abandon_refuses_a_drop_update_that_moves_its_target(upgraded_plan, key):
    plan, _ = upgraded_plan
    changed = _with(plan, "masking_functions_drop", lambda c: c["after"]["input"].__setitem__(key, "changed"))
    assert pg.plan_violations(changed, "abandon") == [
        "module.data_access.terraform_data.masking_functions_drop: update "
        "(abandon only deletes the unfinished change's policies)"]


@pytest.mark.parametrize("actions", [["delete"], ["delete", "create"], ["create", "delete"]])
@pytest.mark.parametrize("stage", ["abandon", "create", "switch", "retire"])
def test_every_stage_refuses_destroying_the_drop(upgraded_plan, stage, actions):
    plan, _ = upgraded_plan
    dropped = _with(plan, "masking_functions_drop", lambda c: c.__setitem__("actions", actions))
    assert any("masking_functions_drop" in p for p in pg.plan_violations(dropped, stage))
    assert not pg.masking_rekey(next(c for c in dropped["resource_changes"]
                                     if c["address"].endswith("masking_functions_drop")))


def test_a_rekey_with_a_destroy_time_drop_on_masking_functions_is_refused(upgraded_plan, monkeypatch):
    plan, _ = upgraded_plan
    monkeypatch.setattr(pg, "redeploy_drops_functions", lambda: True)
    assert pg.plan_violations(plan, "abandon")


def test_unknown_or_missing_deploy_triggers_are_not_a_rekey(upgraded_plan):
    plan, _ = upgraded_plan
    unknown = _with(plan, "terraform_data.masking_functions",
                    lambda c: c["after"]["triggers_replace"].pop("sql_hash"))
    assert pg.plan_violations(unknown, "abandon")
    no_client = _with(plan, "terraform_data.masking_functions",
                      lambda c: [c[s]["triggers_replace"].pop("client_id") for s in ("before", "after")])
    assert pg.plan_violations(no_client, "abandon")


def test_the_upgraded_state_then_plans_nothing_from_either_checkout(upgraded_plan, tmp_path):
    _, new = upgraded_plan
    new.apply()
    other = Layer(tmp_path, checkout(tmp_path, "checkout-c"), new.state, Path(new.env["MASKING_STUB_LOG"]))
    assert _changes(other.plan_json(tmp_path)) == {}


# -- the plan note release, promote and make plan print -------------------------------------
def test_the_plan_note_explains_the_rekey(upgraded_plan, tmp_path):
    _, new = upgraded_plan
    result = new.run("plan")
    assert result.returncode == 0, result.stderr
    out = tmp_path / "plan.txt"
    out.write_text(result.stdout)
    note = subprocess.run([sys.executable, str(SHARED / "scripts" / "masking_replace_note.py"), str(out)],
                          text=True, capture_output=True, check=True).stdout
    assert "it never drops live mask functions" in note
    assert "only stops recording checkout paths" in note
    # A real SQL change gets the general note only.
    (new.env_dir / "masking_functions.sql").write_text(SQL.replace("'***'", "'####'"))
    out.write_text(new.run("plan").stdout)
    note = subprocess.run([sys.executable, str(SHARED / "scripts" / "masking_replace_note.py"), str(out)],
                          text=True, capture_output=True, check=True).stdout
    assert "it never drops live mask functions" in note and "checkout paths" not in note
