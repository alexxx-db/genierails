"""The optional AI helpers (design 18.4, decision 5).

suggest-treatment, draft-genie-content and draft-custom-mask are the only
commands that call a model; test-custom-mask runs the mask library's
exact-output checks on a draft. All of them write only envs/<env>/drafts/,
refuse in CI and in prod or promoted envs, and the model endpoint comes from
the env's config. Every test uses a fake model and a fake warehouse: no network.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from argparse import Namespace
from decimal import Decimal
from pathlib import Path

import pytest

from deterministic_governance import validate_config
from mask_library import load_library
from scripts import ai_helpers
from scripts.live_mask_library import TO_JSON_OPTIONS

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "shared"
ENDPOINT = "databricks-claude-sonnet-4-6"
DEV_ENV = f'''governance_mode = "deterministic"
deployer_principal_id = "d0000000-0000-0000-0000-00000000d001"
access_tier_groups = ["t1", "t2", "t3"]
sql_warehouse_id = "wh-dev"
ai_helper_endpoint = "{ENDPOINT}"
'''
AGENT_SETTINGS = 'genie_space_id = "s1"\nacl_groups = ["t1"]\n'
CAPTURE = {"data_sources": {"tables": [{"identifier": "dev_fin.pay.customers"}]}}
DDL = "-- Table: dev_fin.pay.customers\nCREATE TABLE dev_fin.pay.customers (\n  customer_id BIGINT,\n  region STRING\n);\n"
GENIE_REPLY = {
    "instructions": ["Amounts are in AUD."],
    "sample_questions": ["How many customers are in each region?"],
    "benchmarks": [{"question": "Customers per region", "sql": "SELECT region, count(*) FROM dev_fin.pay.customers GROUP BY region;"}],
}
MASK_REPLY = {
    "description": "Keep the first two characters of a postcode.",
    "sql": "CASE WHEN value IS NULL THEN NULL WHEN length(trim(value)) < 4 THEN '[REDACTED]' ELSE concat(left(trim(value), 2), '**') END",
    "cases": [
        {"input": "CAST('2000' AS STRING)", "expected": "20**", "note": "ordinary"},
        {"input": "CAST('3141' AS STRING)", "expected": "31**", "note": "ordinary"},
        {"input": "CAST('x' AS STRING)", "expected": "[REDACTED]", "note": "malformed"},
    ],
}
CI_MARKERS = ["CI", "GITHUB_ACTIONS", "TF_BUILD", "JENKINS_URL", "GITLAB_CI", "BUILDKITE", "CIRCLECI"]


class FakeModel:
    """Replies with canned JSON and records every prompt."""

    def __init__(self, reply, endpoint=ENDPOINT):
        self.endpoint, self.reply, self.prompts = endpoint, reply, []

    def complete(self, system, prompt):
        self.prompts.append((system, prompt))
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply)


def _no_model(*_args, **_kwargs):
    pytest.fail("this command must stop before it reaches the model")


@pytest.fixture(autouse=True)
def _not_ci(monkeypatch):
    for name in CI_MARKERS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def envs(tmp_path):
    envs = tmp_path / "aws" / "envs"
    dev = envs / "dev"
    (dev / "generated").mkdir(parents=True)
    (dev / "agents").mkdir()
    (dev / "ddl").mkdir()
    (dev / "env.auto.tfvars").write_text(DEV_ENV)
    (dev / "generated" / "abac.auto.tfvars").write_text("groups = {}\n")
    (dev / "agents" / "Payments.auto.tfvars").write_text(AGENT_SETTINGS)
    (dev / "agents" / "Payments.space.json").write_text(json.dumps(CAPTURE))
    (dev / "ddl" / "_fetched.sql").write_text(DDL)
    prod = envs / "prod"
    prod.mkdir()
    (prod / "env.auto.tfvars").write_text(DEV_ENV.replace("d001", "d002"))
    staging = envs / "staging"
    staging.mkdir()
    (staging / "env.auto.tfvars").write_text(DEV_ENV + 'promote_from = "dev"\n')
    for name in ("qa", "sandbox"):  # real, valid, non-promoted envs: still not dev
        (envs / name).mkdir()
        (envs / name / "env.auto.tfvars").write_text(DEV_ENV)
    return envs


def _snapshot(env_dir):
    """Every file outside drafts/."""
    return {str(p.relative_to(env_dir)): p.read_bytes() for p in sorted(env_dir.rglob("*"))
            if p.is_file() and p.relative_to(env_dir).parts[0] != "drafts"}


def _args(envs, command, env="dev", **extra):
    values = dict(class_name="", space="", treatment="", type="", force=False)
    values.update(extra)
    return Namespace(command=command, env=env, env_dir=envs / env, envs_dir=envs, cloud_dir=str(envs.parent), **values)


def _run(envs, command, model, env="dev", **extra):
    return ai_helpers.run(_args(envs, command, env, **extra), client_factory=lambda _d, _c: model)


# ── suggest-treatment ───────────────────────────────────────────────────────

def test_suggest_treatment_writes_only_a_draft(envs):
    dev = envs / "dev"
    before = _snapshot(dev)
    model = FakeModel({"treatment": "redact", "rationale": "Loyalty IDs identify a person.", "custom_mask": None})

    assert _run(envs, "suggest-treatment", model, class_name="loyalty_id") == 0

    draft = json.loads((dev / "drafts" / "treatments" / "loyalty_id.json").read_text())
    assert draft["class"] == "class.loyalty_id"
    assert draft["suggested_treatment"] == "redact"
    # The versions come from the library, not the model.
    assert draft["library_versions"]["full"] == load_library()["treatments"]["redact"]["full"]
    assert 'add "class.loyalty_id" = "redact" to tag_treatments' in draft["to_adopt"]
    assert draft["drafted_by"]["model_endpoint"] == ENDPOINT
    # tag_treatments, generated/ and every other file are untouched.
    assert _snapshot(dev) == before
    assert [p.name for p in (dev / "drafts").rglob("*") if p.is_file()] == ["loyalty_id.json"]
    assert "class.loyalty_id" in model.prompts[0][1]


def test_suggest_treatment_can_point_at_a_custom_mask(envs):
    model = FakeModel({"treatment": None, "rationale": "No library version keeps the area.",
                       "custom_mask": {"name": "postcode_area", "type": "string", "description": "first two digits"}})
    assert _run(envs, "suggest-treatment", model, class_name="class.postcode") == 0
    draft = json.loads((envs / "dev" / "drafts" / "treatments" / "postcode.json").read_text())
    assert draft["suggested_treatment"] is None
    assert draft["custom_mask"] == {"name": "custom_postcode_area", "type": "STRING", "description": "first two digits"}
    assert "make draft-custom-mask ENV=dev TREATMENT=custom_postcode_area TYPE=STRING" in draft["to_adopt"]


@pytest.mark.parametrize("env_text, class_name, message", [
    (DEV_ENV, "email_address", "already mapped by the mask library (to email_partial)"),
    (DEV_ENV + 'tag_treatments = { "class.loyalty_id" = "redact" }\n', "loyalty_id", "already mapped in envs/dev/env.auto.tfvars"),
    (DEV_ENV, "Bad Name", "is not a class name"),
])
def test_suggest_treatment_refuses_a_mapped_or_bad_class_before_the_model(envs, env_text, class_name, message):
    (envs / "dev" / "env.auto.tfvars").write_text(env_text)
    model = FakeModel({})
    with pytest.raises(ai_helpers.Refused, match=re.escape(message)):
        _run(envs, "suggest-treatment", model, class_name=class_name)
    assert model.prompts == []
    assert not (envs / "dev" / "drafts").exists()


@pytest.mark.parametrize("reply, message", [
    ({"treatment": "mask_loyalty", "rationale": "r"}, "not a mask-library treatment"),
    ({"treatment": "redact"}, "no rationale"),
    ("not json", "not one JSON object"),
    ({"treatment": None, "rationale": "r"}, "no custom_mask"),
])
def test_suggest_treatment_refuses_an_unusable_reply_and_writes_nothing(envs, reply, message):
    with pytest.raises(ai_helpers.Refused, match=re.escape(message)):
        _run(envs, "suggest-treatment", FakeModel(reply), class_name="loyalty_id")
    assert not (envs / "dev" / "drafts").exists()


def test_a_changed_draft_needs_force(envs):
    first = FakeModel({"treatment": "redact", "rationale": "one", "custom_mask": None})
    second = FakeModel({"treatment": "redact", "rationale": "two", "custom_mask": None})
    _run(envs, "suggest-treatment", first, class_name="loyalty_id")
    path = envs / "dev" / "drafts" / "treatments" / "loyalty_id.json"
    with pytest.raises(ai_helpers.Refused, match="FORCE=1"):
        _run(envs, "suggest-treatment", second, class_name="loyalty_id")
    assert json.loads(path.read_text())["rationale"] == "one"
    _run(envs, "suggest-treatment", second, class_name="loyalty_id", force=True)
    assert json.loads(path.read_text())["rationale"] == "two"


# ── draft-genie-content ─────────────────────────────────────────────────────

def test_draft_genie_content_drafts_what_the_agent_lacks_and_never_edits_it(envs):
    dev = envs / "dev"
    before = _snapshot(dev)
    model = FakeModel(GENIE_REPLY)

    assert _run(envs, "draft-genie-content", model, space="Payments") == 0

    draft = json.loads((dev / "drafts" / "genie" / "Payments.json").read_text())
    assert draft["drafted"] == {
        "instructions": ["Amounts are in AUD."],
        "sample_questions": ["How many customers are in each region?"],
        "benchmarks": [{"question": "Customers per region",
                        "sql": "SELECT region, count(*) FROM dev_fin.pay.customers GROUP BY region"}],
    }
    assert draft["tables"] == ["dev_fin.pay.customers"]
    assert 'make capture ENV=dev SPACE="Payments"' in draft["to_adopt"]
    assert _snapshot(dev) == before
    # The prompt carries the schema, never data.
    assert '"customer_id": "BIGINT"' in model.prompts[0][1]


def test_draft_genie_content_drafts_only_the_missing_parts(envs):
    capture = dict(CAPTURE, instructions={"text_instructions": [{"content": ["Use AUD."]}]},
                   config={"sample_questions": [{"question": ["Top regions?"]}]})
    (envs / "dev" / "agents" / "Payments.space.json").write_text(json.dumps(capture))
    model = FakeModel({"benchmarks": GENIE_REPLY["benchmarks"]})
    _run(envs, "draft-genie-content", model, space="Payments")
    draft = json.loads((envs / "dev" / "drafts" / "genie" / "Payments.json").read_text())
    assert list(draft["drafted"]) == ["benchmarks"]
    assert draft["not_drafted"] == ["instructions", "sample_questions"]
    assert "Draft only benchmarks" in model.prompts[0][1]


def test_draft_genie_content_refusals(envs):
    with pytest.raises(ai_helpers.Refused, match="has no capture"):
        _run(envs, "draft-genie-content", FakeModel({}), space="Missing")
    full = dict(CAPTURE, instructions={"text_instructions": [{"content": "a"}]},
                config={"sample_questions": [{"question": "q"}]}, benchmarks={"questions": [{"question": "b"}]})
    (envs / "dev" / "agents" / "Payments.space.json").write_text(json.dumps(full))
    with pytest.raises(ai_helpers.Refused, match="nothing to draft"):
        _run(envs, "draft-genie-content", FakeModel({}), space="Payments")
    assert not (envs / "dev" / "drafts").exists()


# ── draft-custom-mask and test-custom-mask ──────────────────────────────────

def test_draft_custom_mask_writes_the_sql_and_its_exact_output_fixture(envs):
    dev = envs / "dev"
    before = _snapshot(dev)
    assert _run(envs, "draft-custom-mask", FakeModel(MASK_REPLY), treatment="custom_postcode_area", type="string") == 0
    sql_path, json_path, passed = ai_helpers.mask_paths(dev, "custom_postcode_area")
    assert ai_helpers.mask_expression(sql_path.read_text()) == MASK_REPLY["sql"]
    fixture = json.loads(json_path.read_text())
    assert fixture["treatment"] == "custom_postcode_area" and fixture["type"] == "STRING"
    # The behaviour contract's NULL case is always in the fixture.
    assert fixture["cases"][0] == {"input": "CAST(NULL AS STRING)", "expected": None, "note": "NULL in gives NULL out"}
    assert fixture["cases"][1:] == MASK_REPLY["cases"]
    assert not passed.exists()
    assert _snapshot(dev) == before


@pytest.mark.parametrize("sql, message", [
    ("CASE WHEN is_account_group_member('t1') THEN value ELSE '[REDACTED]' END", "caller-independent"),
    ("CASE WHEN current_user() = 'x' THEN value END", "caller-independent"),
    ("concat(value, rand())", "deterministic"),
    ("(SELECT max(region) FROM dev_fin.pay.customers)", "not a query or statement"),
    ("value; DROP TABLE x", "one expression"),
    ("'[REDACTED]'", "must read the column as value"),
    # A '--' or '/*' inside a literal can't hide what follows it.
    ("concat(value, '--') || current_user()", "caller-independent"),
    ("concat(value, '/*') || is_member('t1') || '*/'", "caller-independent"),
    ("concat(value, 'open", "every literal and comment closed"),
])
def test_draft_custom_mask_refuses_sql_that_breaks_the_library_rules(envs, sql, message):
    with pytest.raises(ai_helpers.Refused, match=re.escape(message)):
        _run(envs, "draft-custom-mask", FakeModel(dict(MASK_REPLY, sql=sql)), treatment="custom_postcode_area", type="STRING")
    assert not (envs / "dev" / "drafts").exists()


@pytest.mark.parametrize("treatment, sql_type, message", [
    ("redact", "STRING", "is a mask-library treatment"),
    ("Bad-Name", "STRING", "is not a custom mask name"),
    ("custom_postcode_area", "BOOLEAN", "only ever gets the full"),
    ("custom_postcode_area", "ARRAY<STRING>", "only scalar types"),
])
def test_draft_custom_mask_refuses_bad_names_and_types_before_the_model(envs, treatment, sql_type, message):
    with pytest.raises(ai_helpers.Refused, match=re.escape(message)):
        ai_helpers.run(_args(envs, "draft-custom-mask", treatment=treatment, type=sql_type), client_factory=_no_model)


class FakeWarehouse:
    """Answers each fixture statement with a value; records every statement."""

    def __init__(self, answers):
        self.answers, self.statements = answers, []
        self.statement_execution = self

    def execute_statement(self, *, warehouse_id, statement, wait_timeout):
        import types

        self.statements.append((warehouse_id, statement))
        literal = statement.split(" FROM (SELECT ", 1)[1].rsplit(" AS value)", 1)[0]
        value = self.answers[literal]
        rendered = str(value) if isinstance(value, Decimal) else json.dumps(value)
        ok = types.SimpleNamespace(state="SUCCEEDED", error=None)
        return types.SimpleNamespace(status=ok, result=types.SimpleNamespace(data_array=[['{"v": ' + rendered + '}']]))


def _drafted_mask(envs, reply=MASK_REPLY, sql_type="STRING"):
    _run(envs, "draft-custom-mask", FakeModel(reply), treatment="custom_postcode_area", type=sql_type)
    fixture = json.loads(ai_helpers.mask_paths(envs / "dev", "custom_postcode_area")[1].read_text())
    return {case["input"]: case["expected"] for case in fixture["cases"]}


def test_test_custom_mask_passes_exact_outputs_and_records_the_digests(envs):
    dev = envs / "dev"
    answers = _drafted_mask(envs)
    warehouse = FakeWarehouse(answers)
    result = ai_helpers.check_custom_mask(dev, "custom_postcode_area", (warehouse, "wh-dev"))
    assert result == {"passed": True, "cases": 4, "problems": [], "mismatches": []}
    # Read-only and typed: one SELECT per case, the library's cast and non-UTC rendering.
    assert [w for w, _ in warehouse.statements] == ["wh-dev"] * 4
    for _wh, statement in warehouse.statements:
        assert statement.startswith(f"SELECT to_json(named_struct('v', CAST(({MASK_REPLY['sql']}) AS STRING)), {TO_JSON_OPTIONS}) FROM (SELECT ")
    sql_path, json_path, passed = ai_helpers.mask_paths(dev, "custom_postcode_area")
    record = json.loads(passed.read_text())
    assert record["sql_sha256"] == hashlib.sha256(sql_path.read_bytes()).hexdigest()
    assert record["fixture_sha256"] == hashlib.sha256(json_path.read_bytes()).hexdigest()
    assert ai_helpers.passed_record(dev, "custom_postcode_area") == record
    # Editing the draft after it passed invalidates the record.
    sql_path.write_text(sql_path.read_text().replace("'**'", "'##'"))
    assert ai_helpers.passed_record(dev, "custom_postcode_area") is None


def test_test_custom_mask_fails_on_any_mismatch_and_leaves_no_record(envs):
    dev = envs / "dev"
    answers = _drafted_mask(envs)
    ai_helpers.check_custom_mask(dev, "custom_postcode_area", (FakeWarehouse(answers), "wh"))
    answers["CAST('2000' AS STRING)"] = "2000"  # the warehouse disagrees with the fixture
    result = ai_helpers.check_custom_mask(dev, "custom_postcode_area", (FakeWarehouse(answers), "wh"))
    assert result["passed"] is False
    assert result["mismatches"] == [{"input": "CAST('2000' AS STRING)", "expected": "20**", "actual": "2000"}]
    assert not ai_helpers.mask_paths(dev, "custom_postcode_area")[2].exists()


def test_test_custom_mask_runs_the_static_rules_before_the_warehouse(envs):
    dev = envs / "dev"
    _drafted_mask(envs)
    sql_path, json_path, _passed = ai_helpers.mask_paths(dev, "custom_postcode_area")
    sql_path.write_text(sql_path.read_text().replace("WHEN value IS NULL", "WHEN is_account_group_member('t1') THEN value WHEN value IS NULL"))
    fixture = json.loads(json_path.read_text())
    fixture["cases"] = [c for c in fixture["cases"] if c["expected"] is not None]
    json_path.write_text(json.dumps(fixture))
    result = ai_helpers.check_custom_mask(dev, "custom_postcode_area", (_no_model, "wh"))
    assert result["passed"] is False
    assert any("caller-independent" in p for p in result["problems"])
    assert any("NULL in gives NULL out" in p for p in result["problems"])


def test_test_custom_mask_numeric_uses_try_cast_and_exact_decimals(envs):
    reply = {"description": "round to tens", "sql": "round(try_cast(value AS DECIMAL(38, 18)), -1)",
             "cases": [{"input": "CAST(1234 AS INT)", "expected": 1230}, {"input": "CAST(-15 AS INT)", "expected": -20}]}
    dev = envs / "dev"
    answers = _drafted_mask(envs, reply, "INT")
    warehouse = FakeWarehouse({k: (Decimal(str(v)) if v is not None else None) for k, v in answers.items()})
    assert ai_helpers.check_custom_mask(dev, "custom_postcode_area", (warehouse, "wh"))["passed"] is True
    assert all("TRY_CAST((round(" in s for _w, s in warehouse.statements)


def test_test_custom_mask_compares_decimal_fixture_values_exactly(envs):
    reply = {"description": "one decimal place", "sql": "round(try_cast(value AS DECIMAL(38, 18)), 1)",
             "cases": [{"input": "CAST(1.25 AS DOUBLE)", "expected": 1.3}, {"input": "CAST(-1.25 AS DOUBLE)", "expected": -1.3}]}
    answers = _drafted_mask(envs, reply, "DOUBLE")
    warehouse = FakeWarehouse({k: (Decimal(str(v)) if v is not None else None) for k, v in answers.items()})
    assert ai_helpers.check_custom_mask(envs / "dev", "custom_postcode_area", (warehouse, "wh"))["passed"] is True
    answers["CAST(1.25 AS DOUBLE)"] = 1.2
    warehouse = FakeWarehouse({k: (Decimal(str(v)) if v is not None else None) for k, v in answers.items()})
    result = ai_helpers.check_custom_mask(envs / "dev", "custom_postcode_area", (warehouse, "wh"))
    assert result["passed"] is False and result["problems"] == []
    assert [m["input"] for m in result["mismatches"]] == ["CAST(1.25 AS DOUBLE)"]


def test_test_custom_mask_needs_a_warehouse_and_a_draft(envs):
    with pytest.raises(ai_helpers.Refused, match="run make draft-custom-mask"):
        ai_helpers.check_custom_mask(envs / "dev", "custom_postcode_area")
    _drafted_mask(envs)
    (envs / "dev" / "env.auto.tfvars").write_text(DEV_ENV.replace('sql_warehouse_id = "wh-dev"\n', ""))
    with pytest.raises(ai_helpers.Refused, match="set sql_warehouse_id"):
        ai_helpers.check_custom_mask(envs / "dev", "custom_postcode_area")


def test_tag_treatments_cant_name_a_drafted_custom_mask(envs):
    answers = _drafted_mask(envs)
    assert ai_helpers.check_custom_mask(envs / "dev", "custom_postcode_area", (FakeWarehouse(answers), "wh"))["passed"]
    assert ai_helpers.passed_record(envs / "dev", "custom_postcode_area")
    # Even after the draft passes, tag_treatments can't name it until it is adopted.
    errors = validate_config({"access_tier_groups": ["t1"], "tag_treatments": {"class.postcode": "custom_postcode_area"}})
    assert len(errors) == 1
    assert "'custom_postcode_area', which is not adopted in this env's custom_masks/" in errors[0]
    assert "make adopt-custom-mask ENV=dev TREATMENT=custom_postcode_area" in errors[0]
    assert "governance never reads drafts/" in errors[0]


# ── Guards ──────────────────────────────────────────────────────────────────

COMMANDS = [("suggest-treatment", {"class_name": "loyalty_id"}), ("draft-genie-content", {"space": "Payments"}),
            ("draft-custom-mask", {"treatment": "custom_postcode_area", "type": "STRING"}),
            ("test-custom-mask", {"treatment": "custom_postcode_area"})]


@pytest.mark.parametrize("command, extra", COMMANDS)
@pytest.mark.parametrize("marker, value", [("CI", "true"), ("CI", "1"), ("GITHUB_ACTIONS", "true"), ("TF_BUILD", "True"),
                                           ("GITLAB_CI", "true")])
def test_every_helper_refuses_in_ci(envs, monkeypatch, command, extra, marker, value):
    monkeypatch.setenv(marker, value)
    monkeypatch.setenv("GENIERAILS_ALLOW_CI_APPLY", "1")  # the integration-test opt-out never applies here
    with pytest.raises(ai_helpers.Refused, match=f"{marker} is set, and the AI helpers never run in CI"):
        ai_helpers.run(_args(envs, command, **extra), client_factory=_no_model)


@pytest.mark.parametrize("command, extra", COMMANDS)
@pytest.mark.parametrize("env, message", [
    ("prod", "ENV=prod is not allowed (it is promoted)"),
    ("staging", "ENV=staging is not allowed (the AI helpers run only in the dev env)"),
    ("account", "ENV=account is not allowed"),
    # Real, valid envs with no promote_from: still refused, only dev drafts.
    ("qa", "ENV=qa is not allowed (the AI helpers run only in the dev env)"),
    ("sandbox", "ENV=sandbox is not allowed (the AI helpers run only in the dev env)"),
    ("missing", "ENV=missing is not allowed"),
])
def test_every_helper_runs_only_in_dev(envs, command, extra, env, message):
    (envs / "account").mkdir()
    (envs / "account" / "env.auto.tfvars").write_text("")
    with pytest.raises(ai_helpers.Refused, match=re.escape(message)):
        ai_helpers.run(_args(envs, command, env=env, **extra), client_factory=_no_model)


def test_helpers_refuse_a_redirected_env_dir(envs):
    args = _args(envs, "suggest-treatment", class_name="loyalty_id")
    args.env_dir = envs / "prod"
    with pytest.raises(ai_helpers.Refused, match="is not envs/dev of this checkout"):
        ai_helpers.run(args, client_factory=_no_model)


@pytest.mark.parametrize("change, message", [
    ("cloud_dir", "is not envs/dev of this checkout"),         # CLOUD_ROOT pointed at another tree
    ("no_cloud_dir", "run it through the cloud Makefile"),
    ("dev_link", "is a symlink or not a directory"),         # envs/dev -> another dir
    ("envs_link", "is a symlink or not a directory"),        # envs -> another dir
    ("dev_promoted", "envs/dev is promoted"),
])
def test_dev_must_be_this_checkouts_real_unpromoted_envs_dev(envs, tmp_path, change, message):
    args = _args(envs, "suggest-treatment", class_name="loyalty_id")
    elsewhere = tmp_path / "elsewhere"
    if change == "cloud_dir":
        shutil.copytree(envs, elsewhere / "envs")
        args.envs_dir, args.env_dir = elsewhere / "envs", elsewhere / "envs" / "dev"
    elif change == "no_cloud_dir":
        args.cloud_dir = ""
    elif change == "dev_link":
        (envs / "dev").rename(elsewhere)
        (envs / "dev").symlink_to(elsewhere)
    elif change == "envs_link":
        envs.rename(elsewhere)
        envs.symlink_to(elsewhere)
    else:
        (envs / "dev" / "env.auto.tfvars").write_text(DEV_ENV + 'promote_from = "qa"\n')
    with pytest.raises(ai_helpers.Refused, match=re.escape(message)):
        ai_helpers.run(args, client_factory=_no_model)


@pytest.mark.parametrize("command, extra", COMMANDS[:3])
def test_drafting_without_an_endpoint_is_a_clear_error(envs, command, extra):
    (envs / "dev" / "env.auto.tfvars").write_text(DEV_ENV.replace(f'ai_helper_endpoint = "{ENDPOINT}"\n', ""))
    with pytest.raises(ai_helpers.Refused) as error:
        ai_helpers.run(_args(envs, command, **extra))
    message = str(error.value)
    assert f"make {command} needs a model endpoint" in message
    assert "has no ai_helper_endpoint" in message and 'ai_helper_endpoint = "databricks-claude-sonnet-4-6"' in message
    assert "generate, rehearse, release and maintain never call a model" in message
    assert not (envs / "dev" / "drafts").exists()


def test_the_model_client_itself_refuses_in_ci(monkeypatch, tmp_path):
    monkeypatch.setenv("CI", "true")
    with pytest.raises(ai_helpers.Refused, match="never run in CI"):
        ai_helpers.DatabricksModel(ENDPOINT, tmp_path / "auth.auto.tfvars")


def test_ai_helper_endpoint_is_a_valid_env_setting_and_terraform_variable():
    assert validate_config({"ai_helper_endpoint": ENDPOINT}) == []
    for root in ("data_access", "workspace"):
        assert 'variable "ai_helper_endpoint"' in (SHARED / "roots" / root / "main.tf").read_text()


# ── Review blockers (PR #150) ───────────────────────────────────────────────

NOT_LITERALS = [
    "current_user()", "IDENTIFIER('current_user')", "'x' || secret('scope', 'key')",
    "'x' || http_request(conn => 'c', method => 'GET', path => '/')", "ѕelect 1", "(SELECT 1)",
    "`select`", "value", "CAST(value AS STRING)", "CAST(current_user() AS STRING)", "'a' -- comment",
    "'a' /* c */", "'a\\' || current_user() || '", "1; DROP TABLE t", "1 + 1", "concat('a', 'b')",
    "CAST('x' AS ARRAY<STRING>)", "X'00'", "'café'", "is_account_group_member('t1')", "ai_query('m', 'x')",
]


@pytest.mark.parametrize("bad", NOT_LITERALS)
def test_fixture_inputs_must_be_typed_literals_and_never_run(envs, bad):
    dev = envs / "dev"
    answers = _drafted_mask(envs)
    json_path = ai_helpers.mask_paths(dev, "custom_postcode_area")[1]
    fixture = json.loads(json_path.read_text())
    fixture["cases"].append({"input": bad, "expected": "x"})
    json_path.write_text(json.dumps(fixture))
    warehouse = FakeWarehouse(answers)
    result = ai_helpers.check_custom_mask(dev, "custom_postcode_area", (warehouse, "wh"))
    assert result["passed"] is False
    assert any("must be one typed SQL literal" in problem for problem in result["problems"])
    assert warehouse.statements == []  # nothing ran, not even the valid cases
    assert not ai_helpers.mask_paths(dev, "custom_postcode_area")[2].exists()


@pytest.mark.parametrize("bad", NOT_LITERALS[:6])
def test_a_drafted_fixture_with_a_non_literal_input_is_never_written(envs, bad):
    reply = dict(MASK_REPLY, cases=MASK_REPLY["cases"] + [{"input": bad, "expected": "x"}])
    with pytest.raises(ai_helpers.Refused, match="must be one typed SQL literal"):
        _run(envs, "draft-custom-mask", FakeModel(reply), treatment="custom_postcode_area", type="STRING")
    assert not (envs / "dev" / "drafts").exists()


@pytest.mark.parametrize("good", [
    "NULL", "CAST(NULL AS STRING)", "'it''s'", "CAST('-- not a comment' AS STRING)", "12.5", "-1E3",
    "DATE'2024-01-01'", "TIMESTAMP'2024-12-31 20:00:00-08:00'", "TIMESTAMP_NTZ'2024-12-31 23:30:00'",
    "CAST('9223372036854775807' AS BIGINT)", "CAST(12.5 AS DECIMAL(10,2))", "CAST('NaN' AS DOUBLE)",
])
def test_typed_literals_are_accepted(good):
    assert ai_helpers.typed_literal(good) == good.strip()


@pytest.mark.parametrize("sql, rule", [
    ("concat(value, secret('scope', 'key'))", "free of secrets, network and data access: secret"),
    ("concat(value, http_request(conn => 'c', method => 'GET', path => '/'))", "http_request"),
    ("ai_query('endpoint', value)", "must not call a model"),
    ("concat(value, IDENTIFIER('x'))", "identifier"),
    ("concat(value, 'ѕ')", "must be ASCII"),
])
def test_the_mask_itself_cant_read_secrets_call_out_or_resolve_names(sql, rule):
    assert any(rule in problem for problem in ai_helpers.expression_problems(sql))


def _outside(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    return outside


@pytest.mark.parametrize("link", ["drafts", "drafts/treatments"])
def test_a_symlinked_drafts_folder_is_refused_and_nothing_lands_outside(envs, tmp_path, link):
    dev, outside = envs / "dev", _outside(tmp_path)
    (dev / link).parent.mkdir(parents=True, exist_ok=True)
    (dev / link).symlink_to(outside)
    model = FakeModel({"treatment": "redact", "rationale": "r", "custom_mask": None})
    with pytest.raises(ai_helpers.Refused, match=f"envs/dev/{link} is a symlink"):
        _run(envs, "suggest-treatment", model, class_name="loyalty_id")
    assert list(outside.rglob("*")) == []


def test_a_symlinked_draft_file_is_refused_even_with_force(envs, tmp_path):
    dev, outside = envs / "dev", _outside(tmp_path)
    target = outside / "victim.json"
    target.write_text("keep")
    (dev / "drafts" / "treatments").mkdir(parents=True)
    (dev / "drafts" / "treatments" / "loyalty_id.json").symlink_to(target)
    model = FakeModel({"treatment": "redact", "rationale": "r", "custom_mask": None})
    with pytest.raises(ai_helpers.Refused, match="loyalty_id.json is a symlink"):
        _run(envs, "suggest-treatment", model, class_name="loyalty_id", force=True)
    assert target.read_text() == "keep"


@pytest.mark.parametrize("leaf", ["custom_postcode_area.sql", "custom_postcode_area.json", "custom_postcode_area.passed.json"])
def test_test_custom_mask_never_reads_writes_or_removes_through_a_symlink(envs, tmp_path, leaf):
    dev, outside = envs / "dev", _outside(tmp_path)
    answers = _drafted_mask(envs)
    path = dev / "drafts" / "custom_masks" / leaf
    target = outside / leaf
    target.write_text(path.read_text() if path.exists() else "keep")
    path.unlink(missing_ok=True)
    path.symlink_to(target)
    before = target.read_text()
    with pytest.raises(ai_helpers.Refused, match=f"{leaf} is a symlink"):
        ai_helpers.check_custom_mask(dev, "custom_postcode_area", (FakeWarehouse(answers), "wh"))
    assert target.read_text() == before


def test_draft_genie_content_refuses_a_symlinked_capture(envs, tmp_path):
    dev, outside = envs / "dev", _outside(tmp_path)
    capture = dev / "agents" / "Payments.space.json"
    (outside / "c.json").write_text(capture.read_text())
    capture.unlink()
    capture.symlink_to(outside / "c.json")
    with pytest.raises(ai_helpers.Refused, match="Payments.space.json is a symlink"):
        _run(envs, "draft-genie-content", FakeModel(GENIE_REPLY), space="Payments")


def test_drafts_are_staged_so_a_failed_write_changes_nothing(envs, monkeypatch):
    dev = envs / "dev"
    _drafted_mask(envs)
    sql_path, json_path, _ = ai_helpers.mask_paths(dev, "custom_postcode_area")
    before = (sql_path.read_text(), json_path.read_text())
    real_fsync, calls = os.fsync, []

    def failing_fsync(fd):
        calls.append(fd)
        if len(calls) == 2:
            raise OSError("disk full")
        real_fsync(fd)

    monkeypatch.setattr(ai_helpers.os, "fsync", failing_fsync)
    reply = dict(MASK_REPLY, sql=MASK_REPLY["sql"].replace("'**'", "'##'"))
    with pytest.raises(OSError, match="disk full"):
        _run(envs, "draft-custom-mask", FakeModel(reply), treatment="custom_postcode_area", type="STRING", force=True)
    assert (sql_path.read_text(), json_path.read_text()) == before
    assert sorted(p.name for p in sql_path.parent.iterdir()) == ["custom_postcode_area.json", "custom_postcode_area.sql"]


SENTINEL = "SENTINEL-SECRET-4242"


def test_draft_genie_content_sends_only_schema_never_existing_text(envs):
    capture = dict(CAPTURE,
                   instructions={"text_instructions": [{"content": [f"Customer {SENTINEL} is VIP."]}]},
                   config={"sample_questions": [{"question": [f"What did {SENTINEL} buy?"]}]})
    (envs / "dev" / "agents" / "Payments.space.json").write_text(json.dumps(capture))
    model = FakeModel({"benchmarks": GENIE_REPLY["benchmarks"]})
    _run(envs, "draft-genie-content", model, space="Payments")
    (system, prompt), = model.prompts
    assert SENTINEL not in system + prompt
    assert "VIP" not in prompt and "buy" not in prompt
    assert "Payments" not in prompt  # not even the agent's name
    assert '"customer_id": "BIGINT"' in prompt and "dev_fin.pay.customers" in prompt
    assert '["instructions", "sample_questions"]' in prompt  # only which parts exist


@pytest.mark.parametrize("marker", ["TEAMCITY_VERSION", "TRAVIS", "BITBUCKET_BUILD_NUMBER", "CODEBUILD_BUILD_ID",
                                    "BUILD_BUILDID", "DRONE", "SEMAPHORE", "APPVEYOR"])
def test_other_ci_runners_are_refused_too(envs, monkeypatch, marker):
    monkeypatch.setenv(marker, "1")
    with pytest.raises(ai_helpers.Refused, match=f"{marker} is set"):
        ai_helpers.run(_args(envs, "suggest-treatment", class_name="loyalty_id"), client_factory=_no_model)


# ── Real make ───────────────────────────────────────────────────────────────

if shutil.which("make") is None:
    pytest.skip("make is required", allow_module_level=True)

FAKE_SDK = {
    "databricks/__init__.py": "",
    "databricks/sdk/useragent.py": "",
    "databricks/sdk/service/__init__.py": "",
    "databricks/sdk/__init__.py": '''import json, os
from databricks.sdk.service.serving import ServingEndpointsAPI


class WorkspaceClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.serving_endpoints = ServingEndpointsAPI(kwargs)
''',
    "databricks/sdk/service/serving.py": '''import json, os, types


class ChatMessageRole:
    SYSTEM, USER = "system", "user"


class ChatMessage:
    def __init__(self, role, content):
        self.role, self.content = role, content


class ServingEndpointsAPI:
    def __init__(self, auth):
        self.auth = auth

    def query(self, name, messages, **kwargs):
        with open(os.environ["FAKE_MODEL_LOG"], "a") as log:
            log.write(json.dumps({"endpoint": name, "host": self.auth.get("host")}) + "\\n")
        message = types.SimpleNamespace(content=open(os.environ["FAKE_MODEL_REPLY"]).read())
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])
''',
}


@pytest.fixture
def repo(tmp_path, envs):
    repo = tmp_path
    shutil.copy2(ROOT / "aws" / "Makefile", repo / "aws" / "Makefile")
    (repo / "shared").symlink_to(SHARED)
    (envs / "dev" / "auth.auto.tfvars").write_text('databricks_workspace_host = "https://dev.example.invalid"\n')
    for rel, text in FAKE_SDK.items():
        path = tmp_path / "fake_sdk" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return repo


def _make(repo, *args, ci=None, reply=None):
    drop = ("MAKEFLAGS", "MAKELEVEL", "MFLAGS", "ENV", "ENV_DIR", "GENIERAILS_ALLOW_CI_APPLY", *CI_MARKERS)
    env = {k: v for k, v in os.environ.items() if k not in drop and not k.startswith("DATABRICKS_")}
    reply_file = repo / "reply.json"
    reply_file.write_text(json.dumps(reply or {}))
    env.update(PYTHONPATH=str(repo / "fake_sdk"), PIP_NO_INDEX="1", PYTHONDONTWRITEBYTECODE="1",
               FAKE_MODEL_LOG=str(repo / "model.log"), FAKE_MODEL_REPLY=str(reply_file), HOME=str(repo))
    if ci:
        env[ci[0]] = ci[1]
    return subprocess.run(["make", "--no-print-directory", "-C", str(repo / "aws"), *args],
                          text=True, capture_output=True, env=env, timeout=120)


def _model_calls(repo):
    log = repo / "model.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_make_suggest_treatment_calls_the_configured_endpoint_and_writes_a_draft(repo):
    reply = {"treatment": "redact", "rationale": "Identifies a person.", "custom_mask": None}
    before = _snapshot(repo / "aws" / "envs" / "dev")
    result = _make(repo, "suggest-treatment", "ENV=dev", "CLASS=loyalty_id", reply=reply)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DRAFTED envs/dev/drafts/treatments/loyalty_id.json" in result.stdout
    assert _model_calls(repo) == [{"endpoint": ENDPOINT, "host": "https://dev.example.invalid"}]
    assert _snapshot(repo / "aws" / "envs" / "dev") == before
    assert "Upgrading databricks-sdk" not in result.stdout + result.stderr


def test_make_draft_genie_content_and_draft_custom_mask(repo):
    result = _make(repo, "draft-genie-content", "ENV=dev", "SPACE=Payments", reply=GENIE_REPLY)
    assert result.returncode == 0, result.stdout + result.stderr
    result = _make(repo, "draft-custom-mask", "ENV=dev", "TREATMENT=custom_postcode_area", "TYPE=STRING", reply=MASK_REPLY)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "make test-custom-mask ENV=dev TREATMENT=custom_postcode_area" in result.stdout
    drafts = sorted(str(p.relative_to(repo / "aws" / "envs")) for p in (repo / "aws" / "envs").rglob("drafts/**/*") if p.is_file())
    assert drafts == ["dev/drafts/custom_masks/custom_postcode_area.json", "dev/drafts/custom_masks/custom_postcode_area.sql",
                      "dev/drafts/genie/Payments.json"]
    assert len(_model_calls(repo)) == 2


@pytest.mark.parametrize("target, extra", [
    ("suggest-treatment", ["CLASS=loyalty_id"]), ("draft-genie-content", ["SPACE=Payments"]),
    ("draft-custom-mask", ["TREATMENT=custom_postcode_area", "TYPE=STRING"]), ("test-custom-mask", ["TREATMENT=custom_postcode_area"]),
])
@pytest.mark.parametrize("ci", [("CI", "true"), ("TF_BUILD", "True"), ("GITHUB_ACTIONS", "true")])
def test_make_targets_refuse_in_ci(repo, target, extra, ci):
    result = _make(repo, target, "ENV=dev", *extra, ci=ci)
    assert result.returncode != 0
    assert "never run in CI" in result.stderr
    assert _model_calls(repo) == [] and not (repo / "aws" / "envs" / "dev" / "drafts").exists()


@pytest.mark.parametrize("args, message", [
    (["ENV=prod", "CLASS=loyalty_id"], "ENV=prod is not allowed"),
    (["ENV=staging", "CLASS=loyalty_id"], "ENV=staging is not allowed"),
    (["ENV=qa", "CLASS=loyalty_id"], "ENV=qa is not allowed (the AI helpers run only in the dev env)"),
    (["ENV=dev", "ENV_DIR=../envs/prod", "CLASS=loyalty_id"], "is not envs/dev of this checkout"),
    (["ENV=dev"], "CLASS=<class> is required"),
])
def test_make_suggest_treatment_refusals(repo, args, message):
    result = _make(repo, "suggest-treatment", *args)
    assert result.returncode != 0
    assert message in result.stderr
    assert _model_calls(repo) == []


def test_make_without_an_endpoint_stops_with_a_clear_error(repo):
    env_file = repo / "aws" / "envs" / "dev" / "env.auto.tfvars"
    env_file.write_text(DEV_ENV.replace(f'ai_helper_endpoint = "{ENDPOINT}"\n', ""))
    result = _make(repo, "suggest-treatment", "ENV=dev", "CLASS=loyalty_id")
    assert result.returncode != 0
    assert "has no ai_helper_endpoint" in result.stderr
    assert _model_calls(repo) == []


def test_make_dry_run_runs_no_helper(repo):
    result = _make(repo, "-n", "suggest-treatment", "ENV=dev", "CLASS=loyalty_id")
    assert result.returncode == 0
    assert _model_calls(repo) == [] and not (repo / "aws" / "envs" / "dev" / "drafts").exists()


@pytest.mark.parametrize("how", ["command-line", "make -e"])
def test_make_ci_from_the_command_line_or_make_e_is_refused(repo, how):
    args = ["suggest-treatment", "ENV=dev", "CLASS=loyalty_id"]
    if how == "command-line":
        result = _make(repo, *args, "CI=true")
    else:
        result = _make(repo, "-e", *args, ci=("CI", "true"))
    assert result.returncode != 0 and "never run in CI" in result.stderr
    assert _model_calls(repo) == []


def test_make_refuses_a_redirected_cloud_root(repo, tmp_path):
    other = tmp_path / "other"
    shutil.copytree(repo / "aws" / "envs", other / "envs")
    result = _make(repo, "suggest-treatment", "ENV=dev", "CLASS=loyalty_id", f"CLOUD_ROOT={other}")
    assert result.returncode != 0 and "is not envs/dev of this checkout" in result.stderr
    assert _model_calls(repo) == [] and not (other / "envs" / "dev" / "drafts").exists()
