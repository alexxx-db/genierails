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
# The real deployer (copied beside it, so its shared/ is the checkout's), with
# only the credential load and the statements stubbed: it logs what it resolved.
HARNESS = """import importlib.util, json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("dmf", os.path.join(here, "_deployer.py"))
dmf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dmf)
def log(**entry):
    with open(os.environ["MASKING_STUB_LOG"], "a") as f:
        f.write(json.dumps(entry) + "\\n")
dmf.load_credentials = lambda auth_file, host: log(step="auth", auth_file=auth_file)
dmf.drop = lambda sql_file, warehouse_id: log(step="drop", sql_file=sql_file)
dmf.deploy = lambda sql_file, warehouse_id: log(step="deploy", sql_file=sql_file)
dmf.main()
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


def checkout(tmp_path: Path, name: str, module_source: Path = MODULE, *, root: str = ROOT,
             real_deployer: bool = False) -> Path:
    """A checkout: shared/{deploy stub, SQL helpers, modules/data_access, roots/data_access, envs/dev}.

    ``real_deployer`` runs the real deploy_masking_functions.py behind HARNESS.
    """
    shared = tmp_path / name / "shared"
    module = shared / "modules" / "data_access"
    module.mkdir(parents=True)
    (module / "main.tf").write_text(SUPPORT + "\n" + masking_blocks(module_source) + "\n")
    shutil.copy(SHARED / "modules" / "data_access" / "normalize_masking_sql.py", module)
    for helper in ("sql_tokenizer.py", "masking_sql_blocks.py"):
        shutil.copy(SHARED / helper, shared / helper)
    (shared / "deploy_masking_functions.py").write_text(HARNESS if real_deployer else STUB)
    if real_deployer:
        shutil.copy(SHARED / "deploy_masking_functions.py", shared / "_deployer.py")
    roots = shared / "roots" / "data_access"
    roots.mkdir(parents=True)
    (roots / "main.tf").write_text(root)
    env = shared / "envs" / "dev" / "data_access"
    env.mkdir(parents=True)
    write_env(env, SQL)
    return shared


def write_env(env: Path, sql: str) -> None:
    env.mkdir(parents=True, exist_ok=True)
    (env / "masking_functions.sql").write_text(sql)
    (env / "auth.auto.tfvars").write_text('databricks_workspace_host = "https://example.invalid"\n')


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
    # The drop records the new sql_hash in place; only a destroy runs it.
    assert _changes(b.plan_json(tmp_path)) == {"module.data_access.terraform_data.masking_functions":
                                               ["delete", "create"],
                                               "module.data_access.terraform_data.masking_functions_drop": ["update"]}
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
    assert argv[argv.index("--expect-sql-hash") + 1]  # the identity the drop checks


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


def _harness_layer(tmp_path: Path, name: str, **kwargs) -> tuple[Layer, Path]:
    state, log = tmp_path / "state" / "terraform.tfstate", tmp_path / "masking.log"
    state.parent.mkdir(parents=True, exist_ok=True)
    return Layer(tmp_path, checkout(tmp_path, name, real_deployer=True, **kwargs), state, log), log


def test_a_checkout_path_with_spaces_deploys_and_drops(tmp_path):
    """Every argument reaches the deployer whole (review B1: the path split at the space)."""
    layer, log = _harness_layer(tmp_path / "rv199 space", "checkout $(x) 'q'")
    layer.apply()
    assert _calls(log) == [{"step": "auth", "auth_file": str(layer.env_dir / "auth.auto.tfvars")},
                           {"step": "deploy", "sql_file": str(layer.env_dir / "masking_functions.sql")}]
    log.unlink()
    result = layer.run("destroy", "-auto-approve")
    assert result.returncode == 0, result.stdout + result.stderr
    # The SQL hashes to what state records, so the drop goes ahead with this checkout's files.
    assert _calls(log) == [{"step": "auth", "auth_file": str(layer.env_dir / "auth.auto.tfvars")},
                           {"step": "drop", "sql_file": str(layer.env_dir / "masking_functions.sql")}]


def test_a_wrong_but_populated_base_dir_refuses_before_it_connects(tmp_path):
    """GENIERAILS_MASKING_BASE_DIR naming another env (review B2): its SQL is not what state deployed."""
    layer, log = _harness_layer(tmp_path, "checkout-a")
    layer.apply()
    other = layer.env_dir.parents[1] / "prod"
    write_env(other / "data_access", SQL.replace("cat.sch", "prodcat.sch"))
    log.unlink()
    result = layer.run("destroy", "-auto-approve", env=layer.env | {"GENIERAILS_MASKING_BASE_DIR": str(other)})
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "is not the SQL the last apply deployed" in output and "nothing was dropped" in output
    assert _calls(log) == []  # no credentials loaded, nothing dropped
    # The drop is still in state; the right base dir then drops this env's functions.
    result = layer.run("destroy", "-auto-approve")
    assert result.returncode == 0, result.stdout + result.stderr
    assert [c["step"] for c in _calls(log)] == ["auth", "drop"]
    assert _calls(log)[-1]["sql_file"] == str(layer.env_dir / "masking_functions.sql")


def _legacy_state(tmp_path: Path, new_checkout: str = "checkout-b") -> tuple[Layer, Path]:
    """State the pre-fix module applied from checkout A (absolute paths, no sql_hash), then A is gone.

    ``new_checkout="checkout-a"`` puts a fresh checkout back at A's path, so the recorded paths exist.
    """
    state, log = tmp_path / "state" / "terraform.tfstate", tmp_path / "masking.log"
    state.parent.mkdir()
    old = Layer(tmp_path, checkout(tmp_path, "checkout-a", PRE_FIX, root=ROOT.replace(NO_PATHS_BASE, "")),
                state, log)
    old.apply()
    shutil.rmtree(old.root.parents[2])
    log.unlink()
    return Layer(tmp_path, checkout(tmp_path, new_checkout, real_deployer=True), state, log), log


def test_a_direct_destroy_of_unmigrated_state_refuses_until_an_apply_records_its_identity(tmp_path):
    """Review B3: the old checkout's files are gone and state has no sql_hash to check a stand-in by."""
    new, log = _legacy_state(tmp_path)
    result = new.run("destroy", "-auto-approve")
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "records no sql_hash" in output and "Run make apply once" in output
    assert "nothing was dropped" in output
    assert _calls(log) == []
    # The apply the message asks for re-keys the drop, running only CREATE OR REPLACE (no drop);
    # the destroy then drops this checkout's.
    new.apply()
    assert _calls(log) == [{"step": "auth", "auth_file": str(new.env_dir / "auth.auto.tfvars")},
                           {"step": "deploy", "sql_file": str(new.env_dir / "masking_functions.sql")}]
    log.unlink()
    result = new.run("destroy", "-auto-approve")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _calls(log) == [{"step": "auth", "auth_file": str(new.env_dir / "auth.auto.tfvars")},
                           {"step": "drop", "sql_file": str(new.env_dir / "masking_functions.sql")}]


def test_a_direct_destroy_of_unmigrated_state_never_drops_another_checkouts_dev(tmp_path):
    """Review r2 B1: a dev checkout whose SQL is another catalog's sits where the gone one was."""
    new, log = _legacy_state(tmp_path)
    write_env(new.env_dir, SQL.replace("cat.sch", "wrongcat.sch"))
    result = new.run("destroy", "-auto-approve")
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "no sql_hash" in output and "nothing was dropped" in output
    assert _calls(log) == []  # no credentials loaded, wrongcat.sch's functions untouched


def test_a_direct_destroy_of_legacy_state_refuses_even_where_its_paths_still_exist(tmp_path):
    """Review r3 B1: A's path is reused by a checkout whose dev SQL is another catalog's."""
    new, log = _legacy_state(tmp_path, new_checkout="checkout-a")
    write_env(new.env_dir, SQL.replace("cat.sch", "wrongcat.sch"))
    result = new.run("destroy", "-auto-approve")
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "records no sql_hash" in output and "nothing was dropped" in output
    assert "is gone" not in output  # the recorded paths exist; they are refused all the same
    assert _calls(log) == []


def test_the_runner_exports_the_base_dir_the_root_records_against():
    runner = (SHARED / "scripts" / "terraform_layer.sh").read_text()
    assert 'GENIERAILS_MASKING_BASE_DIR="$(dirname "$ENV_DIR")"' in runner
    assert "masking_paths_base" in (SHARED / "roots" / "data_access" / "main.tf").read_text()
    assert re.search(r"masking_paths_base\s*=\s*dirname\(var\.env_dir\)",
                     (SHARED / "roots" / "data_access" / "main.tf").read_text())


# -- the deployer's path resolution --------------------------------------------------------
def _dmf():
    sys.path.insert(0, str(SHARED))
    import deploy_masking_functions as dmf
    return dmf


def _resolve(sql_file, auth_file, base_dir, expect_sql_hash="h", relocate_from="", drop=True):
    parsed = __import__("argparse").Namespace(sql_file=sql_file, auth_file=auth_file, base_dir=base_dir,
                                              expect_sql_hash=expect_sql_hash, relocate_from=relocate_from,
                                              drop=drop)
    _dmf().resolve_paths(parsed)
    return parsed.sql_file, parsed.auth_file


def test_relative_paths_resolve_against_the_base_dir_and_absolute_ones_stay(tmp_path):
    assert _resolve("data_access/m.sql", "data_access/auth", str(tmp_path)) == (
        str(tmp_path / "data_access/m.sql"), str(tmp_path / "data_access/auth"))
    # Absolute paths recorded with a sql_hash (a SQL file outside the env dir, e.g.
    # GENIERAILS_LAYER_CONFIG_DIR) are used as they are while they exist; the hash is checked next.
    old = tmp_path / "old"
    write_env(old, SQL)
    paths = (str(old / "masking_functions.sql"), str(old / "auth.auto.tfvars"))
    assert _resolve(*paths, str(tmp_path)) == paths
    assert _resolve("m.sql", "auth", None) == ("m.sql", "auth")  # no --base-dir: as before
    with pytest.raises(SystemExit):
        _resolve("data_access/m.sql", "data_access/auth", "")
    # Relative paths without a recorded sql_hash can't tell this env's SQL from another's.
    with pytest.raises(SystemExit):
        _resolve("data_access/m.sql", "data_access/auth", str(tmp_path), expect_sql_hash="")


def test_a_gone_absolute_path_relocates_only_to_the_same_place_or_refuses(tmp_path, capsys):
    """Under the env dir; under this shared/ (--relocate-from) is the destroy tests' case."""
    gone = "/gone/checkout/shared"
    # By its last two parts under the env dir, when the dir above them is named like the env.
    env = tmp_path / "envs" / "dev"
    write_env(env / "data_access", SQL)
    assert _resolve(f"{gone}/envs/dev/data_access/masking_functions.sql",
                    f"{gone}/envs/dev/data_access/auth.auto.tfvars", str(env)) == (
        str(env / "data_access/masking_functions.sql"), str(env / "data_access/auth.auto.tfvars"))
    # Another env's dir (the env name differs) or nothing there: refuse.
    for base in (tmp_path / "envs" / "prod", tmp_path / "nowhere" / "dev"):
        write_env(tmp_path / "envs" / "prod" / "data_access", SQL)
        with pytest.raises(SystemExit):
            _resolve(f"{gone}/envs/dev/data_access/masking_functions.sql",
                     f"{gone}/envs/dev/data_access/auth.auto.tfvars", str(base))
    assert "nothing was dropped" in capsys.readouterr().err


def test_a_gone_absolute_path_without_a_recorded_hash_is_never_relocated(tmp_path, capsys):
    """Review r2 B1: the same place in another dev checkout may hold another env's SQL."""
    env = tmp_path / "envs" / "dev"
    write_env(env / "data_access", SQL.replace("cat.sch", "wrongcat.sch"))
    with pytest.raises(SystemExit):
        _resolve("/gone/checkout/shared/envs/dev/data_access/masking_functions.sql",
                 "/gone/checkout/shared/envs/dev/data_access/auth.auto.tfvars", str(env), expect_sql_hash="")
    assert "no sql_hash" in capsys.readouterr().err


def test_a_relocation_through_a_symlink_out_of_the_base_dir_refuses(tmp_path, capsys):
    """Review r2 B1: data_access under the env dir links elsewhere; lexically it is under it."""
    env, outside = tmp_path / "envs" / "dev", tmp_path / "outside" / "dev"
    write_env(outside / "data_access", SQL)
    env.mkdir(parents=True)
    (env / "data_access").symlink_to(outside / "data_access", target_is_directory=True)
    with pytest.raises(SystemExit):
        _resolve("/gone/checkout/shared/envs/dev/data_access/masking_functions.sql",
                 "/gone/checkout/shared/envs/dev/data_access/auth.auto.tfvars", str(env))
    assert "which no longer exists" in capsys.readouterr().err


def test_the_identity_check_passes_this_envs_sql_and_refuses_any_other(tmp_path, capsys):
    import hashlib
    sys.path.insert(0, str(SHARED / "modules" / "data_access"))
    from normalize_masking_sql import normalized_definitions
    sql = tmp_path / "m.sql"
    sql.write_text(SQL)
    recorded = hashlib.sha256(normalized_definitions(SQL).encode()).hexdigest()
    _dmf().verify_sql_identity(str(sql), recorded)
    assert pg.masking_sql_hash(SQL) == recorded  # what the staged drop-functions passes
    sql.write_text(SQL.replace("cat.sch", "other.sch"))
    with pytest.raises(SystemExit):
        _dmf().verify_sql_identity(str(sql), recorded)
    assert "is not the SQL the last apply deployed" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        _dmf().verify_sql_identity(str(sql), "")  # older state records none: no identity, no drop
    assert "records no sql_hash" in capsys.readouterr().err


def test_legacy_absolute_paths_that_exist_refuse_without_a_recorded_hash(tmp_path, capsys):
    """Review r3 B1: the path existing says nothing about whose SQL is at it now."""
    old = tmp_path / "envs" / "dev" / "data_access"
    write_env(old, SQL.replace("cat.sch", "wrongcat.sch"))
    paths = (str(old / "masking_functions.sql"), str(old / "auth.auto.tfvars"))
    for base in (str(old.parent), ""):
        with pytest.raises(SystemExit):
            _resolve(*paths, base, expect_sql_hash="")
    assert "records no sql_hash" in capsys.readouterr().err
    assert _resolve(*paths, str(old.parent)) == paths  # with a recorded hash (checked next), as given


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


def test_the_rekey_records_the_drops_sql_hash_but_abandon_refuses_changing_it(upgraded_plan):
    plan, _ = upgraded_plan
    drop = next(c for c in plan["resource_changes"] if c["address"].endswith("masking_functions_drop"))
    assert "sql_hash" not in drop["change"]["before"]["input"] and drop["change"]["after"]["input"]["sql_hash"]
    recorded = _with(plan, "masking_functions_drop",
                     lambda c: c["before"]["input"].__setitem__("sql_hash", "deployed-before"))
    assert pg.plan_violations(recorded, "abandon") == [
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
