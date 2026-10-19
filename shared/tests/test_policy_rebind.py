"""make policy-rebind: re-point a rolled-back policy change at recreated principals, then abandon it.

The live AWS shape: a switch failed at verify-switched and was restored and
verified; then a sandbox cleanup deleted the deployer SP and the tier groups.
Unity Catalog shows the deleted principals as ``unknown`` on every policy that
named them and drops their grants, and the deployer was recreated under a new
ID. Strict abandon refuses (its plan would re-point policies and recreate
grants), so rebind re-applies the restore render with the generated config's
principals first: protection, checked live, then the lost grants.

Runs on the staged-apply harness (tests/test_staged_policy_apply.py): the real
hooks and live check, a fake Terraform layer that also models grants, the
function redeploy and the enforcement wait (fake_access.json).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import policy_generations as pg
from scripts import staged_policy_apply as spa
from tests.test_staged_policy_apply import (
    FAKE_RUNNER, Harness, _after_call, _retag_live, live_policies, live_state, live_tags, released, write_env,
)

VERSIONS = {"email_partial": {"partial": "redacted"}}
DELETED = {"raw", "regional", "masked", "old-sp"}
GRANTS = [
    "catalog_access|cat|raw", "catalog_access|cat|regional", "catalog_access|cat|masked",
    "schema_access|cat.sch|raw", "schema_access|cat.sch|regional", "schema_access|cat.sch|masked",
    "table_access|cat.sch.t|raw", "table_access|cat.sch.t|regional", "table_access|cat.sch.t|masked",
    "terraform_sp_manage_catalog|cat",
]
REBIND_CALLS = ["rebind-protect:plan", "rebind-protect:saved", "inventory:rebind", "agent-coverage",
                "rebind-access:plan", "rebind-access:saved", "inventory:rebind", "verify-access:rebind"]
CI_MARKERS = ("CI", "TF_BUILD", "JENKINS_URL", "GITLAB_CI", "BUILDKITE", "CIRCLECI", "GENIERAILS_ALLOW_CI_APPLY")


@pytest.fixture
def harness(tmp_path, monkeypatch):
    for marker in CI_MARKERS:
        monkeypatch.delenv(marker, raising=False)
    env_dir = tmp_path / "envs" / "dev"
    env_dir.mkdir(parents=True)
    h = Harness(env_dir, monkeypatch, tmp_path)
    yield h
    assert not h.invariant_problems, h.invariant_problems


def access_file(h) -> Path:
    return h.env_dir / "data_access" / "fake_access.json"


def grant_keys(state) -> set[str]:
    return {f"{r['name']}|{i['index_key']}" for r in state["resources"] if r["type"] == "databricks_grant"
            for i in r["instances"]}


def _fake_terraform(h, *args):
    runner = Path(pg.RUNNER)
    env = os.environ | {"LAYER_ENV_DIR": str(h.env_dir / "data_access"),
                        pg.STAGE_CONFIG_ENV: str(pg.stage_config_dir(h.env_dir))}
    subprocess.run([sys.executable, str(runner), "data_access", "dev", *args], env=env, check=True,
                   stdout=subprocess.DEVNULL)


def _seed_access(h, client_id: str) -> None:
    """The grants, functions and wait the earlier applies left (a grant-phase apply of the live render)."""
    access_file(h).write_text(json.dumps({"grants": GRANTS, "client_id": client_id, "gone": []}))
    plan = h.env_dir / "seed.tfplan"
    _fake_terraform(h, "plan", "-input=false", "-var=policy_stage=grant", f"-out={plan}")
    _fake_terraform(h, "apply-plan", str(plan))
    assert grant_keys(live_state(h.env_dir)) == set(GRANTS)


def _delete_principals(h) -> None:
    """The SCIM cleanup: deleted principals read back as unknown; their grants are gone; a new deployer."""
    path = h.env_dir / "data_access" / "terraform.tfstate"
    state = live_state(h.env_dir)
    for policy in state["resources"]:
        if policy["type"] != "databricks_policy_info":
            continue
        for instance in policy["instances"]:
            for field in ("to_principals", "except_principals"):
                instance["attributes"][field] = ["unknown" if p in DELETED else p
                                                 for p in instance["attributes"][field]]
    path.write_text(json.dumps(state))
    gone = [g for g in GRANTS if g.rsplit("|", 1)[-1] in DELETED or g.startswith("terraform_sp_manage_catalog|")]
    access_file(h).write_text(json.dumps({"grants": GRANTS, "client_id": "new-sp", "gone": gone}))


def deleted_principals(h, *, old_deployer: str = ""):
    """A rolled-back switch whose principals were then deleted and recreated (the new deployer in config)."""
    old = released(h, deployer=old_deployer)
    failed = write_env(h.env_dir, versions=VERSIONS, deployer="old-sp")
    h.fail_verify = {"switch"}
    assert h.apply() == 1
    t = h.journal()["transition"]
    assert (t["completed"], t["failed_phase"], bool(t["restored"])) == (["create"], "verify-switched", True)
    _seed_access(h, "old-sp")
    _delete_principals(h)
    fixed = write_env(h.env_dir, versions=VERSIONS, deployer="new-sp")
    assert fixed["fingerprint"] != failed["fingerprint"]
    h.calls.clear()
    h.verify_envs.clear()
    return old, failed, fixed


def _plan_of(h, name: str) -> list[tuple[str, tuple[str, ...]]]:
    plan = json.loads(Path(name).read_text())
    return sorted((c["address"], tuple(c["change"]["actions"])) for c in plan["resource_changes"])


# -- the AWS shape -------------------------------------------------------------------
def test_rebind_reprotects_first_grants_after_and_abandon_then_only_deletes(harness):
    old, failed, fixed = deleted_principals(harness)
    # Strict abandon refuses: its plan would re-point policies and recreate grants.
    with pytest.raises(spa.Refused, match="abandon plan was refused"):
        harness.controller().abandon()
    harness.calls.clear()
    seen = {}

    def after_protect(env):
        state = live_state(harness.env_dir)
        seen["grants"] = grant_keys(state)
        seen["principals"] = {p for policy in live_policies(state)
                              for p in policy["to_principals"] + policy["except_principals"]}
        seen["tags"] = live_tags(state)

    _after_call(harness, "rebind-protect:saved", after_protect)
    assert harness.controller().rebind() == 0
    assert harness.calls == REBIND_CALLS
    # Phase 1 re-pointed every policy and touched no grant and no column.
    assert "unknown" not in seen["principals"] and "new-sp" in seen["principals"]
    lost = {g for g in GRANTS if g.rsplit("|", 1)[-1] in DELETED or g.startswith("terraform_sp_manage")}
    assert seen["grants"] == set(GRANTS) - lost
    assert seen["tags"] == old["tags"]
    # Phase 2 granted exactly what was lost, table SELECT included (it was applied before the deletion).
    state = live_state(harness.env_dir)
    assert grant_keys(state) == set(GRANTS)
    assert live_tags(state) == old["tags"]
    t = harness.journal()["transition"]
    assert t["rebound"]["generation"] == fixed["fingerprint"]
    assert "rebind_incomplete" not in t and "rebind_held" not in t
    assert pg.load_stage_record(harness.env_dir)["stage"] == "rebind"
    # Strict abandon now plans only the change's policy deletes and the restarted wait.
    harness.calls.clear()
    harness.run = Harness.run.__get__(harness)
    assert harness.controller().abandon() == 0
    assert harness.calls == ["abandon:plan", "inventory:rebind", "abandon:saved", "inventory:abandon"]
    created = {p["name"] for p in failed["fgac_policies"]} - {p["name"] for p in old["fgac_policies"]}
    changes = _plan_of(harness, pg.ledger_dir(harness.env_dir) / "abandon" / f"{t['run_id']}.tfplan")
    assert changes == sorted(
        [(f'module.data_access.databricks_policy_info.policies["{n}"]', ("delete",)) for n in created]
        + [("module.data_access.time_sleep.wait_for_policy_enforcement", ("delete", "create"))])
    assert harness.journal()["transition"] is None
    # The corrected config then goes live through the normal staged apply.
    assert harness.apply() == 0
    assert harness.journal()["active"] == fixed["fingerprint"]
    assert live_tags(live_state(harness.env_dir)) == fixed["tags"]


def test_rebind_of_a_pre_marker_journal(harness, capsys):
    deleted_principals(harness)
    journal = harness.journal()
    journal["transition"].pop("restored")
    spa.save_journal(harness.env_dir, journal)
    capsys.readouterr()
    assert harness.controller().rebind() == 0
    assert "journal predates the restore marker" in capsys.readouterr().out
    assert harness.calls == REBIND_CALLS
    assert "restored" not in harness.journal()["transition"]  # the pre-marker shape abandon relies on
    harness.calls.clear()
    assert harness.controller().abandon() == 0


def test_rebind_plans_print_what_each_phase_changes(harness, capsys):
    deleted_principals(harness)
    capsys.readouterr()
    assert harness.controller().rebind() == 0
    out = capsys.readouterr().out
    assert "update module.data_access.databricks_policy_info.policies" in out
    assert "delete/create module.data_access.terraform_data.masking_functions" in out
    assert "held back until the access phase: 10 grant(s)" in out
    assert 'create module.data_access.databricks_grant.table_access["cat.sch.t|raw"]' in out
    assert "Next: make policy-abandon ENV=dev" in out


# -- refusals before anything is applied ---------------------------------------------------
def _unchanged(h, before, state):
    assert h.journal() == before
    assert live_state(h.env_dir) == state


def test_rebind_refuses_a_column_retagged_live(harness, capsys):
    old, failed, _ = deleted_principals(harness)
    column = next(iter(old["tags"]))
    _retag_live(harness.env_dir, column, failed["tags"][column])
    harness.live_check = "real"
    before, state = harness.journal(), live_state(harness.env_dir)
    capsys.readouterr()
    with pytest.raises(spa.Refused, match="rebind-protect plan was refused"):
        harness.controller().rebind()
    assert harness.calls == ["rebind-protect:plan"]
    assert f'treatment["columns|{column}|gr_treatment"]: update' in capsys.readouterr().err
    _unchanged(harness, before, state)


@pytest.mark.parametrize("edit", ["drop-created", "add-stray"])
def test_rebind_refuses_a_policy_create_or_delete(harness, capsys, edit):
    _, failed, _ = deleted_principals(harness)
    path = harness.env_dir / "data_access" / "terraform.tfstate"
    state = live_state(harness.env_dir)
    policies = next(r for r in state["resources"] if r["type"] == "databricks_policy_info")
    if edit == "drop-created":  # dropped live: the plan creates it
        name = failed["fgac_policies"][0]["name"]
        policies["instances"] = [i for i in policies["instances"] if i["index_key"] != name]
        expected = f'policies["{name}"]: create'
    else:  # one no generation records: the plan deletes it
        stray = json.loads(json.dumps(policies["instances"][0]))
        stray["index_key"] = "gr_mask_cat_stray"
        stray["attributes"]["match_columns"] = [{"condition": "hasTagValue('gr_treatment', 'unused')"}]
        policies["instances"].append(stray)
        expected = 'policies["gr_mask_cat_stray"]: delete'
    path.write_text(json.dumps(state))
    before = harness.journal()
    capsys.readouterr()
    with pytest.raises(spa.Refused, match="rebind-protect plan was refused"):
        harness.controller().rebind()
    assert expected in capsys.readouterr().err
    _unchanged(harness, before, state)


@pytest.mark.parametrize("config, message", [
    ({"deployer": "new-sp"}, "by the generated config"),
    ({"versions": VERSIONS, "deployer": "new-sp", "columns": ("email", "contact")}, "change their treatment"),
], ids=["policies-renamed", "column-added"])
def test_rebind_refuses_a_config_that_changes_more_than_principals(harness, capsys, config, message):
    deleted_principals(harness)
    write_env(harness.env_dir, **config)
    before, state = harness.journal(), live_state(harness.env_dir)
    capsys.readouterr()
    with pytest.raises(spa.Refused, match="rebind-protect plan was refused"):
        harness.controller().rebind()
    assert harness.calls == ["rebind-protect:plan"] and message in capsys.readouterr().err
    _unchanged(harness, before, state)


def test_rebind_refuses_a_retained_policy_naming_the_replaced_deployer(harness, capsys):
    deleted_principals(harness, old_deployer="old-sp")
    capsys.readouterr()
    with pytest.raises(spa.Refused, match="rebind-protect plan was refused"):
        harness.controller().rebind()
    assert "names old-sp, which the generated config replaced" in capsys.readouterr().err


def test_rebind_refuses_a_change_that_never_switched(harness):
    released(harness)
    write_env(harness.env_dir, versions=VERSIONS)
    harness.fail_verify = {"create"}
    assert harness.apply() == 1
    harness.calls.clear()
    before = harness.journal()
    with pytest.raises(spa.Refused, match="never switched"):
        harness.controller().rebind()
    assert harness.calls == [] and harness.journal() == before


def _markerless_other_shape(t):
    t.pop("restored")
    t["completed"] = ["create", "verify-created", "switch"]


@pytest.mark.parametrize("edit", [
    lambda t: t.update(restoring=True), lambda t: t.update(restore_failed=True),
    lambda t: t.update(restored=None), _markerless_other_shape,
], ids=["restoring", "restore-failed", "marker-null", "markerless-other-shape"])
def test_rebind_refuses_a_switch_that_is_not_a_verified_restore(harness, edit):
    deleted_principals(harness)
    journal = harness.journal()
    edit(journal["transition"])
    spa.save_journal(harness.env_dir, journal)
    with pytest.raises(spa.Refused, match="can't be re-pointed"):
        harness.controller().rebind()
    assert harness.calls == []


def test_rebind_refuses_when_the_last_stage_is_not_the_restore(harness):
    deleted_principals(harness)
    record = pg.load_stage_record(harness.env_dir)
    pg.write_json(pg.stage_record_path(harness.env_dir), record | {"stage": "switch"})
    with pytest.raises(spa.Refused, match="last applied stage is switch"):
        harness.controller().rebind()
    assert harness.calls == []


def test_rebind_refuses_when_the_config_is_the_change_itself(harness):
    deleted_principals(harness)
    write_env(harness.env_dir, versions=VERSIONS, deployer="old-sp")
    with pytest.raises(spa.Refused, match="nothing to re-point"):
        harness.controller().rebind()
    assert harness.calls == []


@pytest.mark.parametrize("marker", [{"CI": "true"}, {"CI": "1"}, {"GITLAB_CI": "x"}, {"TF_BUILD": "True"}])
def test_rebind_refuses_in_ci(harness, monkeypatch, marker):
    deleted_principals(harness)
    for key, value in marker.items():
        monkeypatch.setenv(key, value)
    before = harness.journal()
    with pytest.raises(spa.Refused, match="can't run in CI"):
        harness.controller().rebind()
    assert harness.calls == [] and harness.journal() == before


def test_rebind_refuses_a_promoted_env(harness):
    deleted_principals(harness)
    env_file = harness.env_dir / "env.auto.tfvars"
    env_file.write_text(env_file.read_text() + 'promote_from = "dev"\n')
    with pytest.raises(spa.Refused, match="is promoted"):
        harness.controller().rebind()
    assert harness.calls == []


def test_rebind_cli_refuses_in_ci_with_exit_3(tmp_path):
    env_dir = tmp_path / "envs" / "dev"
    write_env(env_dir)
    env = {k: v for k, v in os.environ.items() if k not in CI_MARKERS} | {"CI": "true"}
    result = subprocess.run([sys.executable, str(pg.SHARED / "scripts" / "staged_policy_apply.py"), "rebind",
                             "--env-dir", str(env_dir), "--env-name", "dev"], env=env, text=True,
                            capture_output=True)
    assert result.returncode == 3 and "can't run in CI" in result.stderr


# -- the function redeploy ------------------------------------------------------------------
def test_rebind_refuses_a_redeploy_that_would_drop_the_functions(harness, monkeypatch, capsys, tmp_path):
    deleted_principals(harness)
    module = (pg.SHARED / "modules" / "data_access" / "main.tf").read_text()
    start = module.index('resource "terraform_data" "masking_functions" {')
    at = module.index('provisioner "local-exec" {', start) + len('provisioner "local-exec" {')
    pre76 = tmp_path / "main.tf"
    pre76.write_text(module[:at] + "\n    when = destroy\n" + module[at:])  # the drop on the redeploy itself
    real = pg.redeploy_drops_functions
    monkeypatch.setattr(pg, "redeploy_drops_functions", lambda: real(pre76))
    before, state = harness.journal(), live_state(harness.env_dir)
    capsys.readouterr()
    with pytest.raises(spa.Refused, match="rebind-protect plan was refused"):
        harness.controller().rebind()
    assert "destroy-time drop of the masking functions" in capsys.readouterr().err
    _unchanged(harness, before, state)


def test_rebind_refuses_a_redeploy_that_changes_the_function_sql(harness, capsys):
    deleted_principals(harness)
    path = harness.env_dir / "data_access" / "terraform.tfstate"
    state = live_state(harness.env_dir)
    functions = next(r for r in state["resources"] if (r["type"], r["name"]) == ("terraform_data", "masking_functions"))
    functions["instances"][0]["attributes"]["triggers_replace"]["sql_hash"] = "deployed-from-other-sql"
    path.write_text(json.dumps(state))
    capsys.readouterr()
    with pytest.raises(spa.Refused, match="rebind-protect plan was refused"):
        harness.controller().rebind()
    assert "changes the function SQL" in capsys.readouterr().err


def test_the_module_redeploy_runs_no_drop():
    """terraform_data.masking_functions has no destroy-time provisioner; the drop lives in masking_functions_drop."""
    assert pg.redeploy_drops_functions() is False


@pytest.mark.parametrize("change, problem", [
    ({"address": "module.data_access.terraform_data.masking_functions_drop", "type": "terraform_data",
      "name": "masking_functions_drop", "change": {"actions": ["delete", "create"]}}, "masking_functions_drop"),
    ({"address": "module.data_access.terraform_data.masking_functions_drop", "type": "terraform_data",
      "name": "masking_functions_drop", "change": {"actions": ["delete"]}}, "masking_functions_drop"),
    ({"address": "module.data_access.databricks_entity_tag_assignment.treatment[\"x\"]",
      "type": "databricks_entity_tag_assignment", "name": "treatment", "change": {"actions": ["update"]}}, "treatment"),
    ({"address": 'module.data_access.databricks_policy_info.policies["p"]', "type": "databricks_policy_info",
      "name": "policies", "index": "p", "change": {"actions": ["update"],
                                                     "before": {"column_mask": {"function_name": "c.s.f1"}},
                                                     "after": {"column_mask": {"function_name": "c.s.f2"}}}},
     "changes its column_mask"),
    ({"address": 'module.data_access.databricks_policy_info.policies["p"]', "type": "databricks_policy_info",
      "name": "policies", "index": "p", "change": {"actions": ["update"],
                                                     "before": {"match_columns": [{"condition": "a"}]},
                                                     "after": {"match_columns": [{"condition": "b"}]}}},
     "changes its match_columns"),
    ({"address": 'module.data_access.databricks_grant.catalog_access["c|g"]', "type": "databricks_grant",
      "name": "catalog_access", "index": "c|g", "change": {"actions": ["create"]}}, "catalog_access"),
    ({"address": "module.data_access.time_sleep.wait_for_policy_enforcement", "type": "time_sleep",
      "name": "wait_for_policy_enforcement", "change": {"actions": ["delete"]}}, "wait_for_policy_enforcement"),
], ids=["drop-replaced", "drop-deleted", "tag", "function-binding", "condition", "grant-in-protect", "wait-deleted"])
def test_protect_checker_refuses(change, problem):
    problems = pg.rebind_violations({"resource_changes": [change]}, "rebind-protect", drops=lambda: False,
                                    coverage={"recorded": [], "mapping": {}, "principals": set()})
    assert problems and problem in problems[0]


@pytest.mark.parametrize("change", [
    {"type": "databricks_grant", "name": "table_access", "index": "c.s.t|new-reader", "change": {"actions": ["create"]}},
    {"type": "databricks_grant", "name": "catalog_access", "index": "c|g", "change": {"actions": ["delete"]}},
    {"type": "databricks_grant", "name": "catalog_access", "index": "c|g", "change": {"actions": ["update"]}},
    {"type": "databricks_policy_info", "name": "policies", "index": "p", "change": {"actions": ["update"]}},
    {"type": "terraform_data", "name": "masking_functions", "change": {"actions": ["delete", "create"]}},
], ids=["unrecorded-select", "grant-delete", "grant-update", "policy", "functions"])
def test_access_checker_refuses(change):
    change = {"address": f"module.data_access.{change['type']}.{change['name']}"} | change
    assert pg.rebind_violations({"resource_changes": [change]}, "rebind-access", {"table_access|c.s.t|raw"})


def test_access_checker_allows_only_the_lost_grants():
    plan = {"resource_changes": [
        {"address": "a", "type": "databricks_grant", "name": "table_access", "index": "c.s.t|raw",
         "change": {"actions": ["create"]}},
        {"address": "b", "type": "databricks_grant", "name": "schema_access", "index": "c.s|raw",
         "change": {"actions": ["create"]}},
        {"address": "c", "type": "databricks_policy_info", "name": "policies", "change": {"actions": ["no-op"]}},
    ]}
    assert pg.rebind_violations(plan, "rebind-access", {"table_access|c.s.t|raw", "schema_access|c.s|raw"}) == []


# -- failures after the first apply ---------------------------------------------------------
def test_a_failed_live_check_after_protect_applies_no_grant(harness, capsys):
    old, _, _ = deleted_principals(harness)
    column = next(iter(old["tags"]))
    policy = old["fgac_policies"][0]["name"]

    def diverge(env):  # Unity Catalog differs from what the protect apply says it did
        path = harness.env_dir / "data_access" / "terraform.tfstate"
        state = live_state(harness.env_dir)
        for resource in state["resources"]:
            for instance in resource["instances"]:
                if instance["index_key"] == policy:
                    instance["attributes"]["to_principals"] = ["unknown"]
        path.write_text(json.dumps(state))

    _after_call(harness, "rebind-protect:saved", diverge)
    harness.live_check = "real"
    capsys.readouterr()
    assert harness.controller().rebind() == 1
    assert harness.calls == ["rebind-protect:plan", "rebind-protect:saved", "inventory:rebind"]
    err = capsys.readouterr().err
    assert "REBIND INCOMPLETE at rebind-protect" in err and "No grant was applied" in err
    lost = {g for g in GRANTS if g.rsplit("|", 1)[-1] in DELETED or g.startswith("terraform_sp_manage")}
    assert not grant_keys(live_state(harness.env_dir)) & lost
    assert live_tags(live_state(harness.env_dir))[column] == old["tags"][column]
    t = harness.journal()["transition"]
    assert t["rebind_incomplete"]["phase"] == "rebind-protect"
    assert t["rebind_incomplete"]["next"].endswith("re-run make policy-rebind ENV=dev.")
    assert "policy-rebind stopped at rebind-protect" in harness.controller().describe_transition(t)
    # The grants the protect phase held back are kept for the next run (the state no longer has the lost
    # table SELECT), bound to a copy of the protect plan that held them back.
    (record,) = t["rebind_held"]
    assert {g for g in record["grants"] if g.startswith("table_access|")} == {
        "table_access|cat.sch.t|masked", "table_access|cat.sch.t|raw", "table_access|cat.sch.t|regional"}
    assert record["digest"] == spa._held_digest(t, record)
    assert (pg.ledger_dir(harness.env_dir) / "rebind" / record["plan"]).is_file()
    with pytest.raises(spa.Refused, match="make policy-rebind of run .* didn't finish"):
        harness.controller().abandon()
    # The printed next step: re-run the rebind. It re-plans from what is live and restores every grant.
    harness.calls.clear()
    harness.live_check = "strict"
    harness.run = Harness.run.__get__(harness)
    assert harness.controller().rebind() == 0
    assert harness.calls == REBIND_CALLS
    assert grant_keys(live_state(harness.env_dir)) == set(GRANTS)
    # Only the injected divergence (seen until the re-run re-pointed it), nothing the rebind did.
    assert harness.invariant_problems and all(
        p == "after rebind-protect: regional sees cat.sch.t.email raw" for p in harness.invariant_problems)
    harness.invariant_problems.clear()
    harness.calls.clear()
    assert harness.controller().abandon() == 0


def test_a_failed_agent_coverage_check_applies_no_grant(harness, capsys):
    deleted_principals(harness)
    harness.agent_coverage_rc = 1
    capsys.readouterr()
    assert harness.controller().rebind() == 1
    assert harness.calls == ["rebind-protect:plan", "rebind-protect:saved", "inventory:rebind", "agent-coverage"]
    assert "without a GenieRails mask" in capsys.readouterr().err
    assert not {g for g in grant_keys(live_state(harness.env_dir)) if g.rsplit("|", 1)[-1] in DELETED}
    assert harness.journal()["transition"]["rebind_incomplete"]["phase"] == "rebind-protect"


def test_a_failed_check_after_access_leaves_the_rebind_incomplete(harness, capsys):
    deleted_principals(harness)
    real_run = harness.run

    def run(argv, env=None, check=False):
        if argv[:2] == ["/bin/sh", "-c"]:  # verify-access fails once (injected)
            harness.calls.append("verify-access:rebind")
            return SimpleNamespace(returncode=1)
        return real_run(argv, env=env, check=check)

    harness.run = run
    capsys.readouterr()
    assert harness.controller().rebind() == 1
    assert harness.calls == REBIND_CALLS
    assert "REBIND INCOMPLETE at rebind-access" in capsys.readouterr().err
    assert harness.journal()["transition"]["rebind_incomplete"]["phase"] == "rebind-access"


def test_a_saved_plan_changed_before_its_apply_is_not_applied(harness, monkeypatch):
    deleted_principals(harness)
    state = live_state(harness.env_dir)
    controller = harness.controller()
    real_save = controller._save

    def save(journal):  # the journal write between the protect plan and its apply
        real_save(journal)
        plan = pg.ledger_dir(harness.env_dir) / "rebind" / f"{journal['transition']['run_id']}-rebind-protect.tfplan"
        if plan.exists() and journal["transition"].get("rebind_incomplete", {}).get("phase") == "rebind-protect":
            plan.write_bytes(plan.read_bytes() + b"\n")

    monkeypatch.setattr(controller, "_save", save)
    assert controller.rebind() == 1
    assert harness.calls == ["rebind-protect:plan"]
    assert live_state(harness.env_dir) == state


def test_status_and_unfinished_message_point_at_rebind(harness):
    deleted_principals(harness)
    with pytest.raises(spa.Refused, match="run make policy-rebind ENV=dev first"):
        harness.apply()


def test_policy_rebind_is_a_make_target_with_the_ci_guard():
    makefile = (pg.SHARED / "Makefile.shared").read_text()
    line = next(l for l in makefile.splitlines() if l.startswith("policy-rebind:"))
    assert "_guard-not-ci-apply" in line and "##" in line
    assert '$(_DEPLOYER_CHECK) "make policy-rebind"' in makefile and '$(_GROUP_CHECK) "make policy-rebind"' in makefile
    assert (pg.SHARED / "scripts" / "policy_hooks" / "rebind-protect").stat().st_mode & 0o111
    assert (pg.SHARED / "scripts" / "policy_hooks" / "rebind-access").stat().st_mode & 0o111


def test_fake_runner_models_the_rebind_variables():
    assert "held_access_grants" in FAKE_RUNNER and "rebind_table_grants" in FAKE_RUNNER



# -- review fixes ---------------------------------------------------------------------------
def _edit_config(h, old, new):
    """Edit the generated config and its split (the same policy text in both)."""
    for path in (h.env_dir / "generated" / "abac.auto.tfvars", h.env_dir / "data_access" / "abac.auto.tfvars"):
        text = path.read_text()
        assert text.count(old) == 1, (path, old)
        path.write_text(text.replace(old, new))


@pytest.mark.parametrize("old, new, message", [
    # Codex's case: a principal added to EXCEPT on the account-users mask (it would see raw).
    ('except_principals = ["raw", "regional", "new-sp"]', 'except_principals = ["masked", "raw", "regional", "new-sp"]',
     "changes its except_principals beyond replacing one recreated principal"),
    # A TO principal dropped (swapped for another tier still named elsewhere).
    ('to_principals    = ["regional"]', 'to_principals    = ["masked"]', "replaces regional with masked"),
], ids=["added-except", "removed-to"])
def test_rebind_refuses_a_config_that_loosens_a_mask(harness, capsys, old, new, message):
    deleted_principals(harness)
    _edit_config(harness, old, new)
    before, state = harness.journal(), live_state(harness.env_dir)
    capsys.readouterr()
    with pytest.raises(spa.Refused, match="rebind-protect plan was refused"):
        harness.controller().rebind()
    assert message in capsys.readouterr().err
    _unchanged(harness, before, state)


def test_the_deployer_replacement_is_the_only_principal_change(harness):
    _, failed, fixed = deleted_principals(harness)
    problems, mapping = pg.principal_replacements(fixed, failed, {"raw", "regional", "masked", "new-sp"})
    assert (problems, mapping) == ([], {"old-sp": "new-sp"})
    # A replacement the env doesn't approve (not a tier group, exempt principal or its deployer) refuses.
    problems, _ = pg.principal_replacements(fixed, failed, {"raw", "regional", "masked"})
    assert problems and "isn't a recreated" in problems[0]


def _aws_like_plan(after_except):
    """One updated account-users mask, live after the deletion (deleted principals read back as unknown)."""
    cond = [{"condition": "hasTagValue('gr_treatment', 'v')"}]
    return {"resource_changes": [{
        "address": 'module.data_access.databricks_policy_info.policies["full"]', "type": "databricks_policy_info",
        "name": "policies", "index": "full", "change": {
            "actions": ["update"],
            "before": {"to_principals": ["account users"], "except_principals": ["unknown", "unknown"],
                       "match_columns": cond},
            "after": {"to_principals": ["account users"], "except_principals": after_except, "match_columns": cond}}}]}


RECORDED = [{"name": "full", "to_principals": ["account users"], "except_principals": ["raw", "old-sp"],
             "match_condition": "hasTagValue('gr_treatment', 'v')"}]
COVERAGE = {"recorded": RECORDED, "mapping": {"old-sp": "new-sp"}, "principals": {"raw", "masked", "new-sp"}}


def test_the_coverage_check_allows_the_recreated_principals_back():
    assert pg.coverage_loss_problems(_aws_like_plan(["raw", "new-sp"]), **COVERAGE) == []


def test_the_coverage_check_refuses_an_added_except():
    problems = pg.coverage_loss_problems(_aws_like_plan(["raw", "new-sp", "masked"]), **COVERAGE)
    assert any("adds" in p and "masked" in p for p in problems)
    assert any("would unmask v for masked" in p for p in problems)


def test_the_coverage_check_refuses_a_removed_to():
    plan = _aws_like_plan(["raw", "new-sp"])
    change = plan["resource_changes"][0]["change"]
    change["before"]["to_principals"] = ["account users", "masked"]
    change["after"]["to_principals"] = ["masked"]
    recorded = [RECORDED[0] | {"to_principals": ["account users", "masked"]}]
    problems = pg.coverage_loss_problems(plan, recorded, COVERAGE["mapping"], COVERAGE["principals"])
    assert any("drops ['account users'] from TO" in p for p in problems)


def test_an_access_plan_creating_an_unheld_grant_is_refused(harness, capsys):
    deleted_principals(harness)

    def new_reader(env):  # the config now names a reader the protect phase never held back
        access = json.loads(access_file(harness).read_text())
        access["grants"].append("schema_access|cat.sch|intruder")
        access_file(harness).write_text(json.dumps(access))

    _after_call(harness, "rebind-protect:saved", new_reader)
    capsys.readouterr()
    assert harness.controller().rebind() == 1
    assert 'schema_access["cat.sch|intruder"]: create' in capsys.readouterr().err
    assert "schema_access|cat.sch|intruder" not in grant_keys(live_state(harness.env_dir))
    assert harness.journal()["transition"]["rebind_incomplete"]["phase"] == "rebind-access"


@pytest.mark.parametrize("redigest", [False, True], ids=["edited", "edited-with-digest"])
def test_a_forged_table_key_in_the_journal_is_refused(harness, redigest):
    deleted_principals(harness)
    harness.agent_coverage_rc = 1  # stop after the protect phase, with the held grants journaled
    assert harness.controller().rebind() == 1
    access = json.loads(access_file(harness).read_text())
    access["grants"].append("table_access|cat.sch.t|intruder")
    access_file(harness).write_text(json.dumps(access))
    journal = harness.journal()
    t = journal["transition"]
    t.setdefault("rebind_table_grants", []).append("cat.sch.t|intruder")  # the key 8c090b5 trusted
    for record in t.get("rebind_held") or []:
        record["grants"].append("table_access|cat.sch.t|intruder")
        if redigest:
            record["digest"] = spa._held_digest(t, record)
    spa.save_journal(harness.env_dir, journal)
    harness.agent_coverage_rc = 0
    harness.calls.clear()
    with pytest.raises(spa.Refused, match="can't be trusted"):
        harness.controller().rebind()
    assert harness.calls == []
    assert "table_access|cat.sch.t|intruder" not in grant_keys(live_state(harness.env_dir))


def test_rebind_refuses_ci_even_with_the_opt_out(harness, monkeypatch):
    deleted_principals(harness)
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GENIERAILS_ALLOW_CI_APPLY", "1")
    with pytest.raises(spa.Refused, match="even with GENIERAILS_ALLOW_CI_APPLY=1"):
        harness.controller().rebind()
    assert harness.calls == []
    with pytest.raises(pg.StageError, match="can't run in CI"):
        pg.run_hook("rebind-protect", {"GENIERAILS_ENV_DIR": str(harness.env_dir), "CI": "true",
                                       "GENIERAILS_ALLOW_CI_APPLY": "1"})
