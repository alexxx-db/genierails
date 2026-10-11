"""The versioned create/switch/retire hooks the step-6 staged-apply controller runs."""

import json
import os
import stat
from pathlib import Path

import hcl2
import pytest

import generate_abac
from governance_policies import apply_model, build_model
from scripts import policy_generations as pg

SHARED = Path(__file__).resolve().parents[1]
TIERS = ["raw", "regional", "masked"]


def _generation(env_dir: Path, versions=None, tiers=TIERS, columns=("email",)):
    """Write a generated deterministic config and return its generation."""
    model = build_model({f"cat.sch.t.{c}": ["ip_address"] for c in columns},
                        {f"cat.sch.t.{c}": "STRING" for c in columns},
                        {"access_tier_groups": tiers, "treatment_versions": versions or {}})
    generated = env_dir / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    (generated / "abac.auto.tfvars").write_text("tag_policies = [\n]\ntag_assignments = [\n]\nfgac_policies = [\n]\n")
    generate_abac.write_deterministic_model(generated / "abac.auto.tfvars", generated / "masking_functions.sql", model)
    layer = env_dir / "data_access"
    layer.mkdir(exist_ok=True)
    (layer / "abac.auto.tfvars").write_text(
        'tag_assignments = [\n  { entity_type = "tables", entity_name = "cat.sch.t", tag_key = "row_scope", tag_value = "anz" }\n]\n'
        'fgac_policies = [\n  { name = "rf", policy_type = "POLICY_TYPE_ROW_FILTER", catalog = "cat", '
        'to_principals = ["regional"], function_name = "f", function_catalog = "cat", function_schema = "sch" }\n]\n')
    return pg.desired_generation(env_dir)


def test_desired_generation_is_stable_and_refuses_legacy_values(tmp_path):
    first = _generation(tmp_path)
    assert pg.desired_generation(tmp_path) == first
    (tmp_path / "generated" / "abac.auto.tfvars").write_text(
        'tag_assignments = [{ entity_type = "columns", entity_name = "c.s.t.x", tag_key = "gr_treatment", tag_value = "redact" }]\n')
    with pytest.raises(pg.StageError, match="not a deterministic generation"):
        pg.desired_generation(tmp_path)


def test_create_adds_the_replacement_and_changes_nothing_live(tmp_path):
    old = _generation(tmp_path)
    new = _generation(tmp_path, versions={"ip_address": {"partial": "redacted"}})
    rendered = pg.render_stage("create", new, old, None)
    names = {p["name"] for p in rendered["fgac_policies"]}
    assert names == {p["name"] for p in old["fgac_policies"] + new["fgac_policies"]}
    assert rendered["tags"] == old["tags"]
    for policy in old["fgac_policies"]:
        assert policy["function_name"] in rendered["functions_sql"]
    for policy in new["fgac_policies"]:
        assert policy["function_name"] in rendered["functions_sql"]


def test_create_keeps_old_principals_and_switch_moves_them_in_place(tmp_path):
    old = _generation(tmp_path)
    moved = _generation(tmp_path, tiers=["raw", "analysts", "masked"])
    assert old["tags"] == moved["tags"]  # a tier move needs no new function or value
    create = {p["name"]: p for p in pg.render_stage("create", moved, old, None)["fgac_policies"]}
    switch = {p["name"]: p for p in pg.render_stage("switch", moved, old, None)["fgac_policies"]}
    partial = next(name for name in create if name.endswith("_partial"))
    assert create[partial]["to_principals"] == ["regional"]
    assert switch[partial]["to_principals"] == ["analysts"]


def test_switch_retags_and_keeps_retained_generations_until_retire(tmp_path):
    retired = _generation(tmp_path, versions={"ip_address": {"partial": "redacted"}})
    old = _generation(tmp_path)
    new = _generation(tmp_path, versions={"ip_address": {"partial": "ip_network"}}, columns=("email", "ip"))
    switch = pg.render_stage("switch", new, old, retired)
    assert switch["tags"] == new["tags"]
    kept = {p["name"] for g in (old, new, retired) for p in g["fgac_policies"]}
    assert {p["name"] for p in switch["fgac_policies"]} == kept
    retire = pg.render_stage("retire", new, old, retired)
    assert {p["name"] for p in retire["fgac_policies"]} == {p["name"] for g in (old, new) for p in g["fgac_policies"]}
    # Functions are never dropped by a stage.
    assert all(p["function_name"] in retire["functions_sql"] for p in retired["fgac_policies"])


def test_write_stage_keeps_row_filters_and_other_tags(tmp_path):
    old = _generation(tmp_path)
    pg.write_stage(tmp_path / "data_access", pg.render_stage("create", old, None, None))
    cfg = hcl2.loads((tmp_path / "data_access" / "abac.auto.tfvars").read_text())
    assert [p["name"] for p in cfg["fgac_policies"]][0] == "rf"
    assert {a["tag_key"] for a in cfg["tag_assignments"]} == {"row_scope"}
    sql = (tmp_path / "data_access" / "masking_functions.sql").read_text()
    assert "USE CATALOG cat;\nUSE SCHEMA sch;" in sql


def _change(rtype, index, *actions, address=None):
    return {"type": rtype, "index": index, "address": address or f"module.data_access.{rtype}.x",
            "change": {"actions": list(actions)}}


def test_plan_check_refuses_every_protected_destroy():
    plan = {"resource_changes": [
        _change("databricks_policy_info", "p1", "delete"),
        _change("databricks_policy_info", "p2", "delete", "create"),
        _change("databricks_entity_tag_assignment", "t", "delete"),
        _change("terraform_data", None, "delete", address="module.data_access.terraform_data.masking_functions_drop"),
        _change("terraform_data", None, "delete", "create", address="module.data_access.terraform_data.masking_functions"),
        _change("databricks_policy_info", "p3", "update"),
    ]}
    problems = pg.plan_violations(plan, "create")
    assert len(problems) == 4 and not any("masking_functions:" in p or "p3" in p for p in problems)
    # retire may delete exactly the retire generation's policies, never replace them.
    assert len(pg.plan_violations(plan, "retire", {"p1", "p2"})) == 3


def _stub_runner(tmp_path: Path, plan: dict) -> Path:
    log = tmp_path / "runner.log"
    runner = tmp_path / "runner.sh"
    runner.write_text(
        "#!/bin/sh\n"
        f"echo \"$@\" >> '{log}'\n"
        f"if [ \"$3\" = show ]; then echo '+ terraform show'; echo '{json.dumps(plan)}'; fi\n"
    )
    runner.chmod(runner.stat().st_mode | stat.S_IEXEC)
    return runner


def _environ(env_dir, fingerprint, old=""):
    return {**os.environ, "GENIERAILS_ENV_DIR": str(env_dir), "GENIERAILS_ENV_NAME": "dev",
            "GENIERAILS_POLICY_FINGERPRINT": fingerprint, "GENIERAILS_OLD_POLICY_FINGERPRINT": old,
            "GENIERAILS_RETIRE_FINGERPRINT": ""}


def test_hook_plans_checks_and_applies_exactly_that_plan(tmp_path, monkeypatch):
    env_dir = tmp_path / "dev"
    gen = _generation(env_dir)
    monkeypatch.setattr(pg, "RUNNER", _stub_runner(tmp_path, {"resource_changes": []}))
    assert pg.run_hook("create", _environ(env_dir, gen["fingerprint"])) == 0
    calls = (tmp_path / "runner.log").read_text().splitlines()
    assert [c.split()[2] for c in calls] == ["plan", "show", "apply-plan"]
    assert (env_dir / "data_access" / ".policy_generations" / f"{gen['fingerprint']}.json").is_file()


def test_hook_refuses_a_destroying_plan_and_applies_nothing(tmp_path, monkeypatch):
    env_dir = tmp_path / "dev"
    gen = _generation(env_dir)
    monkeypatch.setattr(pg, "RUNNER", _stub_runner(
        tmp_path, {"resource_changes": [_change("databricks_policy_info", "live", "delete")]}))
    assert pg.run_hook("switch", _environ(env_dir, gen["fingerprint"])) == 1
    assert "apply-plan" not in (tmp_path / "runner.log").read_text()


def test_hook_refuses_a_stale_fingerprint_or_an_unknown_live_generation(tmp_path, monkeypatch):
    env_dir = tmp_path / "dev"
    gen = _generation(env_dir)
    monkeypatch.setattr(pg, "RUNNER", _stub_runner(tmp_path, {"resource_changes": []}))
    with pytest.raises(pg.StageError, match="controller is applying"):
        pg.run_hook("create", _environ(env_dir, "0" * 16))
    with pytest.raises(pg.StageError, match="no snapshot of live generation"):
        pg.run_hook("create", _environ(env_dir, gen["fingerprint"], old="f" * 16))
    assert not (tmp_path / "runner.log").exists()


@pytest.mark.parametrize("stage", pg.STAGES)
def test_hook_executables_are_argv_programs_for_each_stage(stage):
    hook = SHARED / "scripts" / "policy_hooks" / stage
    assert os.access(hook, os.X_OK)
    assert hook.read_text().rstrip().endswith(f"policy_generations.py\" hook {stage}")


def test_layer_runner_supports_show_and_saved_plan_apply():
    runner = (SHARED / "scripts" / "terraform_layer.sh").read_text()
    assert "  show)\n    CMD=(terraform show \"$@\")" in runner
    assert "  apply-plan)\n" in runner and 'CMD=(terraform apply -input=false "$@")' in runner


def test_snapshot_is_kept_next_to_state_not_in_generated(tmp_path):
    gen = _generation(tmp_path)
    path = pg.save_snapshot(tmp_path, gen)
    assert path.parent == tmp_path / "data_access" / ".policy_generations"
    assert pg.load_snapshot(tmp_path, gen["fingerprint"]) == json.loads(json.dumps(gen))
    assert pg.load_snapshot(tmp_path, "") is None


def test_apply_model_output_is_what_desired_generation_reads(tmp_path):
    gen = _generation(tmp_path)
    cfg = hcl2.loads((tmp_path / "generated" / "abac.auto.tfvars").read_text())
    assert [p["name"] for p in gen["fgac_policies"]] == [p["name"] for p in apply_model(cfg, build_model(
        {"cat.sch.t.email": ["ip_address"]}, {"cat.sch.t.email": "STRING"}, {"access_tier_groups": TIERS},
    )).get("fgac_policies")]
