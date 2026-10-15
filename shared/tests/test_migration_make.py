"""make recipes on a pending legacy -> deterministic migration (issue #146).

The bug this guards against came from tests that called the controller
directly: through make, check-layout and import_existing.sh ran first and
refused, or cleared the legacy treatment tags, before the controller could
adopt them. Here real make runs the real _apply-layer, _release-plan-roots
and plan recipes, the real check-layout and import_existing.sh, and the real
staged policy controller (tests/test_migration.py's MigrationHarness: a fake
Terraform layer, in-process hooks). A fake Databricks SDK and Terraform runner
(tests/fake_uc.py) log, in order, every SQL statement and runner call.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts import environment_lock
from scripts import import_record
from scripts import migration
from scripts import staged_policy_apply as spa
from tests import fake_uc, script_stubs
from tests.test_migration import COLUMN, ENV, LEGACY_VALUE, TAG_KEY, _runtime_bytes, opt_in, write_legacy_state

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "shared"

# Run below make as scripts/staged_policy_apply.py: the real Controller, through MigrationHarness.
CONTROLLER = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, {shared!r})
import pytest
from scripts import migration, policy_generations as pg, staged_policy_apply as spa
from tests.test_migration import MigrationHarness

args = sys.argv[1:]
env_dir = Path(args[args.index("--env-dir") + 1])
events = os.environ["FAKE_UC_EVENTS"]
h = MigrationHarness(env_dir, pytest.MonkeyPatch(), Path(os.environ["HARNESS_TMP"]))
if migration.deployment(env_dir)["legacy"]:
    h.legacy = migration.legacy_generation(migration.deployment(env_dir))
tool = Path(os.environ["HARNESS_TMP"]) / "ordered_state_tool.sh"
# Each terraform state mv/rm, with the migration record as it was at that moment.
tool.write_text("#!/bin/sh\n" + sys.executable + " -c 'import json, os, sys\n"
                "p = os.path.join(sys.argv[1], \"data_access\", \".policy_generations\", \"migration.json\")\n"
                "status = json.load(open(p))[\"status\"] if os.path.exists(p) else None\n"
                "open(sys.argv[2], \"a\").write(json.dumps({{\"src\": \"state-tool\", \"argv\": sys.argv[3:], "
                "\"record\": status}}) + chr(10))' \"" + str(env_dir) + "\" \"" + events + "\" \"$@\"\n"
                "exec \"" + str(pg.RUNNER) + "\" \"$@\"\n")
tool.chmod(0o755)
pg.RUNNER = tool
# make points the hooks' coverage re-check at ROOT_RUNNER; the harness hooks run in-process instead.
os.environ.pop("GENIERAILS_COVERAGE_GATE_RUNNER", None)
controller = h.controller()
with open(events, "a") as f:
    f.write(json.dumps({{"src": "controller", "argv": args}}) + "\n")
try:
    if args[0] == "plan":
        rc = controller.plan(Path(args[args.index("--out-dir") + 1]))
    else:
        rc = getattr(controller, args[0])()
except spa.Refused as exc:
    print(f"ERROR: staged policy apply refused: {{exc}}", file=sys.stderr)
    rc = 3
if h.invariant_problems:
    print(h.invariant_problems, file=sys.stderr)
    rc = rc or 1
sys.exit(rc)
'''

MAKE = r'''#!{python}
"""Recursive make: runs the recipes under test, records and skips the rest."""
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_UC_EVENTS"], "a") as f:
    f.write(json.dumps({{"src": "make", "argv": args}}) + "\n")
if "_prepare-classification" in args or ("_plan-layer" in args and "LAYER=data_access" in args):
    os.execvp("make", ["make", *args])
'''

# Live UC as legacy GenieRails left it: the treatment tag carries the legacy value.
LIVE = {
    "column_tags": [[COLUMN, "gr_treatment", LEGACY_VALUE]],
    "table_tags": [],
    "warehouses": [{"id": "wh", "name": "Shared", "state": "RUNNING"}],
}


@contextmanager
def _held(env_dir: Path, owner: str):
    """The env lock, held by this process as make rehearse / release-plan's _ENV_LOCK holds it."""
    ok, message, nonce = environment_lock.acquire(env_dir, os.getpid(), owner)
    assert ok, message
    os.environ[environment_lock.nonce_var(env_dir)] = nonce
    try:
        yield
    finally:
        os.environ.pop(environment_lock.nonce_var(env_dir), None)
        environment_lock.release_lock(env_dir, os.getpid())


def _with_tag_attributes(env_dir: Path) -> None:
    """Real Terraform state records each tag's entity_type and tag_key (check-layout reads them)."""
    path = env_dir / "data_access" / "terraform.tfstate"
    state = json.loads(path.read_text())
    for resource in state["resources"]:
        if resource["type"] == "databricks_entity_tag_assignment":
            for instance in resource["instances"]:
                instance["attributes"] |= {"entity_type": "columns", "tag_key": "gr_treatment"}
    path.write_text(json.dumps(state))


class Recipes:
    def __init__(self, tmp_path: Path, live: dict, catalogs: str = ""):
        self.tmp = tmp_path
        self.env_dir = tmp_path / "envs" / ENV
        self.env_dir.mkdir(parents=True)
        write_legacy_state(self.env_dir)
        _with_tag_attributes(self.env_dir)
        opt_in(self.env_dir)
        (self.env_dir / "auth.auto.tfvars").write_text('databricks_workspace_host = "https://fake.cloud.databricks.com"\n')
        self.fake = fake_uc.install(tmp_path, live)
        harness = tmp_path / "harness"
        harness.mkdir()
        controller = tmp_path / "controller.py"
        controller.write_text(CONTROLLER.format(shared=str(SHARED)))
        self.make = tmp_path / "make-stub"
        self.make.write_text(MAKE.format(python=sys.executable))
        self.make.chmod(0o755)
        catalogs_stub = tmp_path / "catalogs"
        catalogs_stub.write_text(f"#!/bin/sh\necho '{catalogs}'\n")
        catalogs_stub.chmod(0o755)
        self.bin = script_stubs.install(tmp_path / "stub-bin", {
            "staged_policy_apply.py": script_stubs.python_file(controller),
            "coverage_gate.py": "true", "table_readers.py": "true", "exposure_token.py": "true",
            "prepare_classification_config.py": str(catalogs_stub),
        })
        self.environ = {k: v for k, v in os.environ.items() if k not in ("MAKEFLAGS", "MAKELEVEL", "APPLY_FLAGS")}
        self.environ |= self.fake | {"HARNESS_TMP": str(harness), "PATH": f"{self.bin}:{os.environ['PATH']}",
                                     "GIT_CONFIG_GLOBAL": "/dev/null"}

    def __call__(self, target: str, *extra: str):
        args = ["make", "--no-print-directory", target, f"ENV={ENV}", f"ENV_DIR={self.env_dir}",
                f"ROOT_RUNNER={self.fake['FAKE_UC_RUNNER']}", f"MAKE={self.make}", *extra]
        environ = dict(self.environ)
        nonce = os.environ.get(environment_lock.nonce_var(self.env_dir))
        if nonce:
            environ[environment_lock.nonce_var(self.env_dir)] = nonce
        return subprocess.run(args, cwd=ROOT / "aws", text=True, capture_output=True, env=environ)

    def events(self, src: str | None = None) -> list[dict]:
        return [e for e in fake_uc.events(self.fake) if src is None or e["src"] == src]

    def files(self) -> dict[str, bytes]:
        # Everything but the env lock's own files (generated/.governance.lock/.guard).
        return {str(p.relative_to(self.env_dir)): p.read_bytes() for p in sorted(self.env_dir.rglob("*"))
                if p.is_file() and not p.name.startswith(".governance.")}


def _treatment_writes(recipes: Recipes) -> list:
    unsets = [s for s in fake_uc.sql_writes(recipes.fake) if "UNSET TAGS" in s]
    treatment_imports = [a for a in fake_uc.runner_imports(recipes.fake) if "gr_treatment" in " ".join(a)]
    return unsets + treatment_imports


def test_apply_layer_adopts_a_pending_migration_without_touching_its_treatment_tags(tmp_path):
    recipes = Recipes(tmp_path, LIVE)
    # A stale regular copy of the pre-opt-in env file where the bootstrap symlink should be.
    (recipes.env_dir / "data_access" / "env.auto.tfvars").write_text(
        'governance_mode = "legacy"\nsql_warehouse_id = "wh"\n')
    with _held(recipes.env_dir, "rehearse"):
        result = recipes("_apply-layer", "LAYER=data_access", f"TARGET_ENV={ENV}",
                         f"LAYER_ENV_DIR={recipes.env_dir / 'data_access'}", "IMPORT_CLASSIFICATION=1")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "old Terraform address" not in out and "pending migration" in out
    assert _treatment_writes(recipes) == []
    events = recipes.events()
    first_move = next(i for i, e in enumerate(events) if e["src"] == "state-tool" and e["argv"][2] == "state-mv")
    # check-layout deferred, import_existing.sh ran, then the controller adopted;
    # the no-way-back marker was on disk at the first state move.
    order = [result.stdout.index(line) for line in (
        "pending migration", "Auto-importing existing data_access resources", "=== Migration (dev): adopting")]
    assert order == sorted(order)
    controller = next(i for i, e in enumerate(events) if e["src"] == "controller")
    assert controller < first_move and events[first_move]["record"] == "adopting"
    assert events[first_move]["argv"][3:] == [
        f'module.data_access.databricks_entity_tag_assignment.assignments["{TAG_KEY}"]',
        f'module.data_access.databricks_entity_tag_assignment.treatment["columns|{COLUMN}|gr_treatment"]']
    assert migration.load_record(recipes.env_dir)["status"] == "adopted"


def _release_plan(recipes: Recipes, plan_dir: Path):
    plan_dir.mkdir()
    with _held(recipes.env_dir, "release-plan"):
        return recipes("_release-plan-roots", f"PLAN_DIR={plan_dir}")


def test_release_plan_of_a_pending_migration_saves_only_the_adoption(tmp_path):
    recipes = Recipes(tmp_path, LIVE)
    before, files = _runtime_bytes(recipes.env_dir), recipes.files()
    result = _release_plan(recipes, tmp_path / "plan")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "old Terraform address" not in out
    phases = json.loads((tmp_path / "plan" / "data_access" / spa.PHASES_FILE).read_text())
    assert [s["kind"] for s in phases["steps"]] == ["adopt"] and phases["final"] is False
    assert _runtime_bytes(recipes.env_dir) == before and recipes.files() == files
    assert spa.load_journal(recipes.env_dir) is None and migration.load_record(recipes.env_dir) is None
    assert recipes.events("state-tool") == [] and fake_uc.sql_writes(recipes.fake) == []
    assert fake_uc.runner_imports(recipes.fake) == []
    assert (tmp_path / "plan" / "imports.jsonl").read_text() == ""


def test_release_plan_records_imports_and_stale_tags_instead_of_changing_them(tmp_path):
    """release-plan changes nothing live: a stale non-treatment tag and importable resources are recorded."""
    live = LIVE | {"table_tags": [["cat.sch.t", "sensitivity", "public"], ["cat.sch.t", "owner", "fin"]]}
    recipes = Recipes(tmp_path, live, catalogs="cat")
    env_file = recipes.env_dir / "env.auto.tfvars"
    env_file.write_text(env_file.read_text() + 'sql_warehouse_id = "wh"\n')  # where release clears the stale tag
    layer_abac = recipes.env_dir / "data_access" / "abac.auto.tfvars"
    layer_abac.write_text(layer_abac.read_text().replace("tag_assignments = [", (
        'tag_assignments = [\n'
        '  { entity_type = "tables", entity_name = "cat.sch.t", tag_key = "sensitivity", tag_value = "pii" },\n'
        '  { entity_type = "tables", entity_name = "cat.sch.t", tag_key = "owner", tag_value = "fin" },'), 1))
    before, files = _runtime_bytes(recipes.env_dir), recipes.files()
    result = _release_plan(recipes, tmp_path / "plan")
    assert result.returncode == 0, result.stdout + result.stderr
    assert fake_uc.sql_writes(recipes.fake) == [] and fake_uc.runner_imports(recipes.fake) == []
    assert _runtime_bytes(recipes.env_dir) == before and recipes.files() == files
    entries = import_record.load(tmp_path / "plan" / "imports.jsonl", recipes.env_dir)  # what release accepts
    assert [(e["kind"], e.get("address") or e["statement"]) for e in entries] == [
        ("import", 'module.data_access.databricks_data_classification_catalog_config.classification["cat"]'),
        ("cleanup", "ALTER TABLE `cat`.`sch`.`t` UNSET TAGS ('sensitivity')"),
        ("import", 'module.data_access.databricks_entity_tag_assignment.assignments["tables|cat.sch.t|owner|fin"]'),
    ]
    # This manifest only imports; the controller plans nothing until the next release-plan.
    assert json.loads((tmp_path / "plan" / "data_access" / spa.PHASES_FILE).read_text()) == {
        "steps": [], "final": False, "imports_only": True}
    assert recipes.events("controller") == []
    assert "deferred" in json.loads((tmp_path / "plan" / "workspace.json").read_text())["skipped"]


def test_make_plan_of_a_pending_migration_previews_and_writes_nothing(tmp_path):
    recipes = Recipes(tmp_path, LIVE)
    files = recipes.files()
    result = recipes("plan")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "migrates from the legacy deployment" in out and "old Terraform address" not in out
    assert [e["argv"][0] for e in recipes.events("controller")] == ["describe"]
    assert recipes.files() == files
    assert recipes.events("state-tool") == [] and fake_uc.sql_writes(recipes.fake) == []
    assert fake_uc.runner_imports(recipes.fake) == []


@pytest.mark.parametrize("recipe", ["_apply-layer", "_release-plan-roots"])
def test_a_deterministic_env_with_old_address_tags_and_no_migration_still_refuses(tmp_path, recipe):
    """Not a pending migration (the old-address value is a generation's): both recipes still refuse."""
    recipes = Recipes(tmp_path, LIVE)
    state_path = recipes.env_dir / "data_access" / "terraform.tfstate"
    state = json.loads(state_path.read_text())
    for resource in state["resources"]:
        if resource["name"] == "assignments":
            for instance in resource["instances"]:
                instance["index_key"] = instance["index_key"].replace(LEGACY_VALUE, f"{LEGACY_VALUE}__string__1234abcd")
                instance["attributes"]["tag_value"] = f"{LEGACY_VALUE}__string__1234abcd"
    state_path.write_text(json.dumps(state))
    before = recipes.files()
    if recipe == "_apply-layer":
        with _held(recipes.env_dir, "rehearse"):
            result = recipes(recipe, "LAYER=data_access", f"TARGET_ENV={ENV}",
                             f"LAYER_ENV_DIR={recipes.env_dir / 'data_access'}")
    else:
        result = _release_plan(recipes, tmp_path / "plan")
    assert result.returncode != 0
    assert "old Terraform address" in result.stderr
    assert recipes.files() == before and recipes.events("controller") == []
    assert fake_uc.sql_writes(recipes.fake) == [] and fake_uc.runner_imports(recipes.fake) == []
