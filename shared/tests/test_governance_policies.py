"""Deterministic tier policies: shapes, exclusivity, never-raw, fail-safe and naming."""

import os
import subprocess
from itertools import product
from pathlib import Path

import pytest

from governance_policies import (
    FAILSAFE_TREATMENT, DeterministicGovernanceError, apply_model, base_treatment, build_model,
    build_policies, column_types_from_ddl, expand_principals, model_fingerprint,
    parse_treatment_value, principal_sets_are_disjoint, replace_sql_library, resolve_column,
    sql_library_block,
)
from validate_abac import ValidationResult, validate_coverage_gate

TIERS = ["raw", "regional", "masked"]


def _model(classes, types=None, **settings):
    types = types or {column.lower(): "STRING" for column in classes}
    return build_model(classes, types, {"access_tier_groups": TIERS, **settings},
                       deployer_principal="deployer-sp")


def test_ordinary_treatment_has_partial_and_full_policies_with_the_tier_shape():
    model = _model({"cat.sch.t.email": ["email_address"]})
    partial, full = sorted(model.fgac_policies, key=lambda p: p["name"].endswith("_full"))
    assert partial["to_principals"] == ["regional"]
    assert partial["except_principals"] == ["raw", "deployer-sp"]
    assert full["to_principals"] == ["account users"]
    assert full["except_principals"] == ["raw", "regional", "deployer-sp"]
    value = model.tag_values[0]
    assert partial["function_name"] == f"gr_mask_{value}_partial"
    assert full["function_name"] == f"gr_mask_{value}_full"
    assert partial["match_condition"] == full["match_condition"] == f"hasTagValue('gr_treatment', '{value}')"


def _mask_for(policies, memberships):
    """Which of a value's policies apply to a principal in ``memberships``."""
    principals = set(memberships) | {"account users"}
    return [p["name"].rsplit("_", 1)[1] for p in policies
            if principals & set(p["to_principals"]) and not principals & set(p.get("except_principals") or [])]


@pytest.mark.parametrize("tiers", [["raw"], ["raw", "masked"], ["raw", "a", "masked"], ["raw", "a", "b", "masked"]])
def test_every_principal_gets_at_most_one_mask_and_the_most_privileged_tier(tiers):
    policies = build_policies(values_by_catalog={"cat": ["email_partial__string__0a1b2c3d"]},
                              access_tier_groups=tiers, raw_exempt_principals=["etl"],
                              deployer_principal="sp")
    pool = tiers + ["etl", "sp", "outsider"]
    for flags in product([False, True], repeat=len(pool)):
        member = [name for name, on in zip(pool, flags) if on]
        masks = _mask_for(policies, member)
        assert len(masks) <= 1, (member, masks)
        if {tiers[0], "etl", "sp"} & set(member):
            expected = []
        elif set(tiers[1:-1]) & set(member):
            expected = ["partial"]
        else:
            expected = ["full"]  # the last tier, and anyone outside the tiers
        assert masks == expected, member


def test_partial_and_full_effective_sets_are_disjoint_with_nested_groups():
    members = {"regional": ["alice", "nested"], "nested": ["bob"], "raw": ["admin", "alice"],
               "account users": ["alice", "bob", "admin", "outsider"]}
    assert expand_principals(["regional"], members) >= {"alice", "bob"}
    policies = build_policies(values_by_catalog={"cat": ["email_partial__string__0a1b2c3d"]},
                              access_tier_groups=TIERS)
    partial, full = policies
    assert principal_sets_are_disjoint(partial["to_principals"], partial["except_principals"],
                                       full["to_principals"], full["except_principals"], members)


@pytest.mark.parametrize("classes", [["card_security_code"], ["secret"], ["card_pin", "name"], ["future_class"]])
def test_never_raw_and_unmapped_are_full_for_everyone_including_the_deployer(classes):
    model = _model({"cat.sch.t.c": classes}, raw_exempt_principals=["etl"])
    assert len(model.fgac_policies) == 1
    (policy,) = model.fgac_policies
    assert policy["to_principals"] == ["account users"]
    assert policy["except_principals"] == []
    assert "_partial" not in model.functions_sql


def test_unmapped_class_wins_and_is_reported():
    model = _model({"cat.sch.t.c": ["email_address", "future_class"]})
    assert base_treatment(model.tag_values[0]) == FAILSAFE_TREATMENT
    assert model.unmapped == [("cat.sch.t.c", "future_class")]


def test_never_raw_class_names_the_column_even_when_another_class_is_stricter_by_partial():
    item = resolve_column("c.s.t.x", ["health_data", "card_pin"], "STRING")
    assert item.never_raw and item.treatment == "card_pin"


def test_functions_are_exactly_typed_since_unity_catalog_has_no_overloading():
    model = _model({"cat.sch.t.dob": ["date_of_birth"], "cat.sch.t.amt": ["compensation"]},
                   {"cat.sch.t.dob": "TIMESTAMP_NTZ", "cat.sch.t.amt": "decimal(10, 2)"})
    sql = model.functions_sql
    assert sql.startswith("USE CATALOG cat;\nUSE SCHEMA sch;")
    assert "(value TIMESTAMP_NTZ) RETURNS TIMESTAMP_NTZ" in sql
    assert "(value DECIMAL(10,2)) RETURNS DECIMAL(10,2)" in sql
    names = [line.split("(", 1)[0] for line in sql.splitlines() if line.startswith("CREATE")]
    assert len(names) == len(set(names)) == 4
    assert {parse_treatment_value(v)[1] for v in model.tag_values} == {"timestamp_ntz", "decimal_10_2"}


def test_complex_types_and_deferred_overrides_are_refused():
    with pytest.raises(DeterministicGovernanceError, match="scalar"):
        _model({"cat.sch.t.c": ["email_address"]}, {"cat.sch.t.c": "ARRAY<STRING>"})
    with pytest.raises(DeterministicGovernanceError, match="tier_access_overrides"):
        _model({"cat.sch.t.c": ["email_address"]}, tier_access_overrides={"email_partial": {"regional": "full"}})
    with pytest.raises(DeterministicGovernanceError, match="no column type"):
        build_model({"cat.sch.t.c": ["email_address"]}, {}, {"access_tier_groups": TIERS})


def test_generation_follows_the_mask_sql_not_the_principals():
    base = _model({"cat.sch.t.ip": ["ip_address"]})
    moved = build_model({"cat.sch.t.ip": ["ip_address"]}, {"cat.sch.t.ip": "STRING"},
                        {"access_tier_groups": ["raw", "other", "masked"]})
    assert base.tag_values == moved.tag_values
    changed = _model({"cat.sch.t.ip": ["ip_address"]}, treatment_versions={"ip_address": {"partial": "redacted"}})
    assert changed.tag_values != base.tag_values
    assert base_treatment(changed.tag_values[0]) == "ip_address"
    # Same inputs, same output.
    assert _model({"cat.sch.t.ip": ["ip_address"]}) == base


def test_one_tier_is_raw_only_and_two_tiers_have_no_partial_policy():
    one = build_policies(values_by_catalog={"c": ["email_partial__string__0a1b2c3d"]}, access_tier_groups=["raw"])
    assert [(p["to_principals"], p["except_principals"]) for p in one] == [(["account users"], ["raw"])]
    two = build_policies(values_by_catalog={"c": ["email_partial__string__0a1b2c3d"]},
                         access_tier_groups=["raw", "masked"])
    assert [p["name"].endswith("_full") for p in two] == [True]


def test_column_types_are_read_from_fetched_ddl():
    ddl = """-- fetched
CREATE TABLE cat.sch.orders (
  id BIGINT NOT NULL,
  amount DECIMAL(10, 2) COMMENT 'x',
  tags ARRAY<STRING>,
  meta STRUCT<a: INT, b: STRING>,
  email STRING
) USING delta;
"""
    assert column_types_from_ddl(ddl) == {
        "cat.sch.orders.id": "BIGINT", "cat.sch.orders.amount": "DECIMAL(10, 2)",
        "cat.sch.orders.tags": "ARRAY<STRING>", "cat.sch.orders.meta": "STRUCT<a: INT, b: STRING>",
        "cat.sch.orders.email": "STRING",
    }


def test_apply_model_replaces_masks_and_legacy_sensitivity_tags_but_keeps_row_filters():
    model = _model({"cat.sch.t.email": ["email_address"]})
    cfg = {
        "tag_policies": [{"key": "pii_level", "values": ["masked_email"]}, {"key": "row_scope", "values": ["anz"]}],
        "tag_assignments": [
            {"entity_type": "columns", "entity_name": "cat.sch.t.email", "tag_key": "pii_level", "tag_value": "masked_email"},
            {"entity_type": "tables", "entity_name": "cat.sch.t", "tag_key": "row_scope", "tag_value": "anz"},
        ],
        "fgac_policies": [
            {"name": "legacy", "policy_type": "POLICY_TYPE_COLUMN_MASK"},
            {"name": "rf", "policy_type": "POLICY_TYPE_ROW_FILTER"},
        ],
    }
    out = apply_model(cfg, model)
    assert [p["name"] for p in out["fgac_policies"]][0] == "rf"
    assert all(p["comment"] == "GenieRails deterministic governance policy" for p in out["fgac_policies"][1:])
    assert {a["tag_key"] for a in out["tag_assignments"]} == {"row_scope", "gr_treatment"}
    assert [p["key"] for p in out["tag_policies"]] == ["row_scope", "gr_treatment"]
    assert out["tag_policies"][-1]["values"] == model.tag_values


def test_sql_library_block_round_trips_and_fingerprint_tracks_principals():
    model = _model({"cat.sch.t.email": ["email_address"]})
    sql = replace_sql_library("USE CATALOG cat;\nCREATE FUNCTION legacy(v STRING) RETURNS STRING RETURN v;\n",
                              model.functions_sql)
    assert sql.startswith("USE CATALOG cat;\nCREATE FUNCTION legacy")
    assert sql_library_block(sql) == model.functions_sql
    assert replace_sql_library(sql, model.functions_sql) == sql
    cfg = apply_model({}, model)
    moved = apply_model({}, build_model({"cat.sch.t.email": ["email_address"]}, {"cat.sch.t.email": "STRING"},
                                        {"access_tier_groups": ["raw", "x", "masked"]},
                                        deployer_principal="deployer-sp"))
    assert model_fingerprint(cfg, model.functions_sql) != model_fingerprint(moved, model.functions_sql)
    assert model_fingerprint(cfg, model.functions_sql) == model_fingerprint(dict(cfg), model.functions_sql)


def test_coverage_check_accepts_a_deterministic_model_and_still_blocks_gaps():
    model = _model({"cat.sch.t.email": ["email_address"], "cat.sch.t.c": ["future_class"]})
    cfg = apply_model({}, model)
    functions = {line.split("(", 1)[0].rsplit(" ", 1)[1]
                 for line in model.functions_sql.splitlines() if line.startswith("CREATE")}
    clean = ValidationResult()
    validate_coverage_gate(cfg, functions, "", clean)
    assert clean.errors == []
    # An unmapped class is redacted, but blocks new access until it is mapped.
    marker = ValidationResult()
    validate_coverage_gate(cfg, functions, "# gr.classification_unmapped: cat.sch.t.c|class.future_class\n", marker)
    assert any("no mapping/rule" in error for error in marker.errors)
    missing = ValidationResult()
    validate_coverage_gate(cfg, functions - {f"gr_mask_{model.tag_values[0]}_full"}, "", missing)
    assert any("missing a masking function" in error for error in missing.errors)


ROOT = Path(__file__).resolve().parents[2]


def _layer(tmp_path, target, env_text):
    env_dir = tmp_path / "aws" / "envs" / "dev"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "env.auto.tfvars").write_text(env_text)
    # A runner that fails proves the layer was skipped before Terraform.
    runner = tmp_path / "runner.sh"
    runner.write_text("#!/bin/sh\necho RUNNER-CALLED; exit 7\n")
    runner.chmod(0o755)
    from tests.script_stubs import without_ci

    environ = {k: v for k, v in without_ci(os.environ).items() if k not in {"MAKEFLAGS", "MAKELEVEL"}}
    return subprocess.run(
        ["make", "-f", str(ROOT / "aws" / "Makefile"), target, "LAYER=workspace", "TARGET_ENV=dev",
         f"LAYER_ENV_DIR={env_dir}", f"ROOT_RUNNER={runner}", f"SHARED_ROOT={ROOT / 'shared'}"],
        cwd=ROOT, env=environ, text=True, capture_output=True, timeout=60,
    )


@pytest.mark.parametrize("target", ["_plan-layer", "_apply-layer"])
def test_governance_only_env_skips_the_workspace_layer(tmp_path, target):
    from scripts import environment_lock

    # _apply-layer runs inside make rehearse, under its env lock (release_gate.py apply-guard).
    env_dir = tmp_path / "aws" / "envs" / "dev"
    env_dir.mkdir(parents=True)
    ok, _message, nonce = environment_lock.acquire(env_dir, os.getpid(), "rehearse")
    assert ok
    os.environ[environment_lock.nonce_var(env_dir)] = nonce  # as make's _ENV_LOCK exports it
    try:
        result = _layer(tmp_path, target, 'governance_mode = "deterministic"\nuc_tables = ["cat.sch.t"]\n')
    finally:
        os.environ.pop(environment_lock.nonce_var(env_dir), None)
        environment_lock.release_lock(env_dir, os.getpid())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "governance-only (deterministic, no agents); GenieRails creates and grants nothing" in result.stdout
    assert "RUNNER-CALLED" not in result.stdout


@pytest.mark.parametrize("env_text", [
    'uc_tables = ["cat.sch.t"]\n',  # legacy
    'governance_mode = "deterministic"\ngenie_spaces = [{ name = "a", genie_space_id = "x", acl_groups = [] }]\n',
])
def test_legacy_or_agent_envs_still_reach_the_workspace_layer(tmp_path, env_text):
    result = _layer(tmp_path, "_plan-layer", env_text)
    assert "governance-only" not in result.stdout
