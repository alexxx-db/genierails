"""A deterministic `make generate` never calls a model (design decision 5).

Governance comes from native class.* tags and the mask library, Genie config
from the Genie API import; no model endpoint is configured or needed.
"""

import json
import socket
import sys
from pathlib import Path

import hcl2
import pytest

import generate_abac
from governance_policies import TAG_KEY, replace_sql_library, sql_library_block
from sensitivity_source import ClassificationSource
from tests.test_derive_assignments import MODEL_FUNCTIONS

SPACE_ID = "01ef7b3c2a4d5e6f"
AGENT = "Payments Agent"
TABLES = ["dev_fin.payments.customers", "dev_fin.payments.payments"]
TIERS = ["payments_ops", "regional_analysts", "viewers"]
DEPLOYER = "82bb9d6d-0000-0000-0000-000000000000"
SERIALIZED = json.dumps({
    "data_sources": {"tables": [{"identifier": table} for table in TABLES]},
    "instructions": {"text_instructions": [{"content": ["Amounts are in AUD."]}]},
})
DDL = """-- Table: dev_fin.payments.customers
CREATE TABLE dev_fin.payments.customers (
  customer_id BIGINT,
  email STRING,
  ssn STRING,
  date_of_birth DATE
);

-- Table: dev_fin.payments.payments
CREATE TABLE dev_fin.payments.payments (
  payment_id BIGINT,
  cvv STRING
);"""
TAG_ROWS = [
    ("dev_fin", "payments", "customers", "email", "class.email_address", ""),
    ("dev_fin", "payments", "customers", "ssn", "class.us_ssn", ""),
    ("dev_fin", "payments", "customers", "date_of_birth", "class.date_of_birth", ""),
    ("dev_fin", "payments", "payments", "cvv", "class.card_security_code", ""),
]
ENV = f'''governance_mode = "deterministic"
deployer_principal_id = "{DEPLOYER}"
enable_classification = true
access_tier_groups = {json.dumps(TIERS)}
genie_spaces = [{{ genie_space_id = "{SPACE_ID}", acl_groups = {json.dumps(TIERS)} }}]
'''
# A reviewed legacy draft: its masks are replaced, its row filter, table tag,
# group description and functions are kept.
REVIEWED = '''groups = {
  "payments_ops" = { description = "Reviewed tier 1" }
  "invented_by_model" = { description = "Not a tier" }
}
tag_policies = [
  { key = "region", description = "Row scope", values = ["anz"] },
  { key = "gr_treatment", description = "legacy", values = ["email_partial"] },
]
tag_assignments = [
  { entity_type = "tables", entity_name = "dev_fin.payments.customers", tag_key = "region", tag_value = "anz" },
  { entity_type = "columns", entity_name = "dev_fin.payments.customers.email", tag_key = "gr_treatment", tag_value = "email_partial" },
]
fgac_policies = [
  { name = "legacy_email", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "dev_fin", to_principals = ["viewers"], match_condition = "hasTagValue('gr_treatment', 'email_partial')", match_alias = "c", function_name = "mask_email", function_catalog = "dev_fin", function_schema = "payments" },
  { name = "anz_rows", policy_type = "POLICY_TYPE_ROW_FILTER", catalog = "dev_fin", to_principals = ["regional_analysts"], when_condition = "hasTagValue('region', 'anz')", function_name = "filter_anz", function_catalog = "dev_fin", function_schema = "payments" },
]
'''
REVIEWED_SQL = '''USE CATALOG dev_fin;
USE SCHEMA payments;

CREATE OR REPLACE FUNCTION mask_email(val STRING)
RETURNS STRING
RETURN '***';

CREATE OR REPLACE FUNCTION filter_anz()
RETURNS BOOLEAN
RETURN TRUE;
'''


@pytest.fixture
def env_dir(tmp_path, monkeypatch):
    env_dir = tmp_path / "envs" / "dev"
    (env_dir / "data_access").mkdir(parents=True)
    (env_dir / "env.auto.tfvars").write_text(ENV)
    # Workspace credentials only: no model endpoint, provider key or token.
    (env_dir / "auth.auto.tfvars").write_text(
        'databricks_workspace_host = "https://example.invalid"\n'
        f'databricks_client_id = "{DEPLOYER}"\ndatabricks_client_secret = "secret"\n'
    )
    for name in ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_CLIENT_ID",
                 "DATABRICKS_CLIENT_SECRET", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(env_dir)
    monkeypatch.setattr(generate_abac, "WORK_DIR", env_dir)
    monkeypatch.setattr(generate_abac, "list_account_group_names", lambda cfg: list(TIERS))
    monkeypatch.setattr(
        generate_abac, "fetch_tables_from_genie_space",
        lambda space_id, cfg, quick_check_only=False: (
            list(TABLES), generate_abac.parse_genie_config_from_serialized_space(SERIALIZED),
            AGENT, True,
        ),
    )
    monkeypatch.setattr(generate_abac, "fetch_tables_from_databricks",
                        lambda refs, cfg: (DDL, [("dev_fin", "payments")]))
    monkeypatch.setattr(generate_abac, "_fetch_live_classification_source",
                        lambda *a, **k: ClassificationSource(tag_rows=TAG_ROWS))
    validated = []
    monkeypatch.setattr(generate_abac, "run_validation",
                        lambda directory, **k: validated.append(directory) or True)
    _forbid_model_and_network(monkeypatch)
    return env_dir


def _forbid_model_and_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("deterministic generate must never call a model or the network")

    for name in MODEL_FUNCTIONS + ("configure_databricks_env",):
        monkeypatch.setattr(generate_abac, name, forbidden, raising=False)
    monkeypatch.setattr(generate_abac, "PROVIDERS", {
        name: {"call": forbidden, "default_model": "unused"} for name in generate_abac.PROVIDERS
    })
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _reviewed_draft(env_dir):
    generated = env_dir / "generated"
    generated.mkdir()
    (generated / "abac.auto.tfvars").write_text(REVIEWED)
    (generated / "governed_tables.json").write_text(json.dumps({"tables": TABLES}) + "\n")
    return generated


def _generate(env_dir, monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", [
        "generate_abac.py", "--auth-file", str(env_dir / "auth.auto.tfvars"), *args,
    ])
    generate_abac.main()


def _expected_model(env_dir):
    auth_cfg = generate_abac.load_auth_config(env_dir / "auth.auto.tfvars")
    return generate_abac.deterministic_model_for(
        auth_cfg, ClassificationSource(tag_rows=TAG_ROWS), DDL)


def _by_name(policies):
    # HCL drops an empty except_principals list; the model keeps it.
    return {p["name"]: {k: v for k, v in p.items() if v != []} for p in policies}


def _column_masks(cfg):
    return _by_name(p for p in cfg["fgac_policies"] if p["policy_type"] == "POLICY_TYPE_COLUMN_MASK")


def _treatments(cfg):
    return [a for a in cfg["tag_assignments"]
            if a["entity_type"] == "columns" and a["tag_key"] == TAG_KEY]


def test_generate_calls_no_model_and_needs_no_endpoint(env_dir, monkeypatch, capfd):
    _generate(env_dir, monkeypatch)

    out = capfd.readouterr().out
    assert "FMAPI" not in out and "LLM" not in out
    assert "no model called" in out
    generated = env_dir / "generated"
    assert (generated / "abac.auto.tfvars").exists()
    assert (generated / "masking_functions.sql").exists()
    assert not (generated / "generated_response.md").exists()
    assert not (generated / "TUNING.md").exists()
    assert "TUNING.md" not in out
    assert "kept reviewed rule" not in out


def test_generate_writes_exactly_the_deterministic_model(env_dir, monkeypatch):
    _generate(env_dir, monkeypatch)

    model = _expected_model(env_dir)
    cfg = hcl2.loads((env_dir / "generated/abac.auto.tfvars").read_text())
    assert _column_masks(cfg) == _by_name(model.fgac_policies)
    assert _treatments(cfg) == model.tag_assignments
    sql = (env_dir / "generated/masking_functions.sql").read_text()
    assert sql_library_block(sql) == sql_library_block(replace_sql_library("", model.functions_sql))
    # The deployer SP and tiers come from the env; the groups are the tiers.
    assert any(DEPLOYER in p.get("except_principals", []) for p in model.fgac_policies)
    assert list(cfg["groups"]) == TIERS
    # Genie config is the API import, not a model draft.
    assert cfg["genie_space_configs"][AGENT]["instructions"] == "Amounts are in AUD."
    assert cfg["genie_space_id_to_name"] == {SPACE_ID: AGENT}


def test_rerun_over_a_reviewed_draft_keeps_its_rules_and_is_stable(env_dir, monkeypatch):
    generated = _reviewed_draft(env_dir)
    (generated / "masking_functions.sql").write_text(REVIEWED_SQL)

    _generate(env_dir, monkeypatch)

    model = _expected_model(env_dir)
    first = (generated / "abac.auto.tfvars").read_text()
    cfg = hcl2.loads(first)
    assert _column_masks(cfg) == _by_name(model.fgac_policies)
    assert [p["name"] for p in cfg["fgac_policies"] if p["policy_type"] != "POLICY_TYPE_COLUMN_MASK"] == ["anz_rows"]
    assert {"entity_type": "tables", "entity_name": "dev_fin.payments.customers",
            "tag_key": "region", "tag_value": "anz"} in cfg["tag_assignments"]
    assert cfg["groups"] == {
        "payments_ops": {"description": "Reviewed tier 1"},
        "regional_analysts": {"description": ""},
        "viewers": {"description": ""},
    }
    sql = (generated / "masking_functions.sql").read_text()
    assert "FUNCTION filter_anz()" in sql and "FUNCTION mask_email(" in sql

    _generate(env_dir, monkeypatch)
    assert (generated / "abac.auto.tfvars").read_text() == first


def test_generate_is_byte_stable_after_id_only_agent_is_captured(env_dir, monkeypatch):
    """The capture written after a fresh make generate must not change its rerun."""
    env_file = env_dir / "env.auto.tfvars"
    env_file.write_text(ENV.replace(
        f'genie_spaces = [{{ genie_space_id = "{SPACE_ID}", acl_groups = {json.dumps(TIERS)} }}]\n',
        "",
    ))
    agents = env_dir / "agents"
    agents.mkdir()
    (agents / f"{AGENT}.auto.tfvars").write_text(
        f'genie_space_id = "{SPACE_ID}"\nacl_groups = {json.dumps(TIERS)}\n'
    )
    calls = []

    def fetch(space_id, cfg, quick_check_only=False):
        calls.append(space_id)
        return (
            list(TABLES), generate_abac.parse_genie_config_from_serialized_space(SERIALIZED),
            AGENT, True,
        )

    monkeypatch.setattr(generate_abac, "fetch_tables_from_genie_space", fetch)

    _generate(env_dir, monkeypatch)
    tfvars = env_dir / "generated/abac.auto.tfvars"
    first = tfvars.read_bytes()

    # This is the side effect of make generate's import-new step, which runs
    # after generate_abac on the first invocation; make also records the
    # governed table set before returning.
    (agents / f"{AGENT}.space.json").write_text(SERIALIZED + "\n")
    (env_dir / "generated/governed_tables.json").write_text(
        json.dumps({"tables": TABLES}) + "\n"
    )
    _generate(env_dir, monkeypatch)

    assert tfvars.read_bytes() == first
    assert calls == [SPACE_ID], "a captured agent must not add another Genie API read"


CAPTURE_IDS = {"Alpha": "01aaaaaaaaaaaaaa", "Beta": "01bbbbbbbbbbbbbb"}


def _captured_serialized(name):
    return json.dumps({
        "data_sources": {"tables": [{"identifier": table} for table in TABLES]},
        "instructions": {"text_instructions": [{"content": [f"{name} instr."]}]},
    })


class _CaptureGenerateEnv:
    """Fresh fake make-generate environment for capture transition tests."""

    def __init__(self, tmp_path, monkeypatch, *, legacy=False):
        from tests import test_generate_abac_golden as golden

        self.monkeypatch = monkeypatch
        self.legacy = legacy
        self.legacy_args = golden.LEGACY_ARGS + ["--allow-rule-changes"]
        self.env = tmp_path / "envs" / "dev"
        (self.env / "data_access").mkdir(parents=True)
        (self.env / "agents").mkdir()
        (self.env / "env.auto.tfvars").write_text("" if legacy else ENV.split("genie_spaces", 1)[0])
        (self.env / "auth.auto.tfvars").write_text(golden.AUTH)
        golden._offline(monkeypatch, self.env)
        self.calls = []
        by_id = {space_id: name for name, space_id in CAPTURE_IDS.items()}

        def fetch(space_id, cfg, quick_check_only=False):
            self.calls.append(space_id)
            name = by_id[space_id]
            return (
                list(TABLES),
                generate_abac.parse_genie_config_from_serialized_space(
                    _captured_serialized(name)
                ),
                name,
                True,
            )

        monkeypatch.setattr(generate_abac, "fetch_tables_from_genie_space", fetch)

    def add(self, name, *, stem=None, display_name=None):
        stem = stem or name
        text = (
            f'genie_space_id = "{CAPTURE_IDS[name]}"\n'
            f'acl_groups = {json.dumps(TIERS)}\n'
        )
        if display_name:
            text += f'name = {json.dumps(display_name)}\n'
        (self.env / "agents" / f"{stem}.auto.tfvars").write_text(text)

    def capture(self, stem, *, name=None):
        (self.env / "agents" / f"{stem}.space.json").write_text(
            _captured_serialized(name or stem) + "\n"
        )

    def rename(self, old, new):
        for suffix in (".auto.tfvars", ".space.json"):
            source = self.env / "agents" / f"{old}{suffix}"
            if source.exists():
                source.rename(self.env / "agents" / f"{new}{suffix}")

    def remove(self, name):
        for suffix in (".auto.tfvars", ".space.json"):
            path = self.env / "agents" / f"{name}{suffix}"
            if path.exists():
                path.unlink()

    def generate(self, *args):
        self.calls.clear()
        argv = self.legacy_args if self.legacy else []
        self.monkeypatch.setattr(sys, "argv", [
            "generate_abac.py", "--auth-file", str(self.env / "auth.auto.tfvars"),
            *argv, *args,
        ])
        assert generate_abac.main() in (None, 0)
        (self.env / "generated/governed_tables.json").write_text(
            json.dumps({"tables": TABLES}) + "\n"
        )
        return (self.env / "generated/abac.auto.tfvars").read_bytes(), list(self.calls)


@pytest.mark.parametrize("legacy", [False, True], ids=["deterministic", "legacy"])
@pytest.mark.parametrize("mode", [(), ("--mode", "genie")], ids=["full", "genie"])
@pytest.mark.parametrize("names", [("Alpha",), ("Alpha", "Beta")], ids=["one", "two"])
def test_generate_is_stable_after_all_agents_are_captured(
    tmp_path, monkeypatch, legacy, mode, names,
):
    fake = _CaptureGenerateEnv(tmp_path, monkeypatch, legacy=legacy)
    for name in names:
        fake.add(name)
    first, first_calls = fake.generate(*mode)
    for name in names:
        fake.capture(name)
    second, second_calls = fake.generate(*mode)
    third, third_calls = fake.generate(*mode)

    assert sorted(first_calls) == sorted(CAPTURE_IDS[name] for name in names)
    assert second_calls == third_calls == []
    assert second == third == first


@pytest.mark.parametrize("legacy", [False, True], ids=["deterministic", "legacy"])
def test_generate_is_stable_with_captured_and_uncaptured_agents(tmp_path, monkeypatch, legacy):
    fake = _CaptureGenerateEnv(tmp_path, monkeypatch, legacy=legacy)
    fake.add("Alpha")
    fake.generate()
    fake.capture("Alpha")
    fake.add("Beta")

    first, first_calls = fake.generate()
    second, second_calls = fake.generate()
    third, third_calls = fake.generate()

    assert first_calls == second_calls == third_calls == [CAPTURE_IDS["Beta"]]
    assert first == second == third


def test_captured_agent_matches_by_id_when_display_name_differs_from_stem(tmp_path, monkeypatch):
    fake = _CaptureGenerateEnv(tmp_path, monkeypatch)
    fake.add("Alpha")
    fake.add("Beta", stem="beta", display_name="Beta")
    first, first_calls = fake.generate()
    fake.capture("Alpha")
    fake.capture("beta", name="Beta")

    second, second_calls = fake.generate()
    third, third_calls = fake.generate()

    assert sorted(first_calls) == sorted(CAPTURE_IDS.values())
    assert second_calls == third_calls == []
    assert first == second == third


def test_deterministic_governance_mode_strips_genie_identity_and_config(tmp_path, monkeypatch):
    fake = _CaptureGenerateEnv(tmp_path, monkeypatch)
    fake.add("Alpha")
    fake.generate()

    first, _ = fake.generate("--mode", "governance")
    second, _ = fake.generate("--mode", "governance")

    assert b"genie_space_id_to_name" not in first
    assert b"genie_space_configs" not in first
    assert second == first


@pytest.mark.parametrize("legacy", [False, True], ids=["deterministic", "legacy"])
def test_captured_agent_rename_and_removal_refresh_mapping(tmp_path, monkeypatch, legacy):
    fake = _CaptureGenerateEnv(tmp_path, monkeypatch, legacy=legacy)
    fake.add("Alpha")
    fake.add("Beta")
    fake.generate()
    fake.capture("Alpha")
    fake.capture("Beta")
    fake.generate()

    fake.rename("Alpha", "Alpha2")
    renamed, rename_calls = fake.generate()
    renamed_cfg = hcl2.loads(renamed.decode())
    assert rename_calls == []
    assert renamed_cfg["genie_space_id_to_name"] == {
        CAPTURE_IDS["Alpha"]: "Alpha2", CAPTURE_IDS["Beta"]: "Beta",
    }

    fake.remove("Beta")
    removed, remove_calls = fake.generate()
    removed_cfg = hcl2.loads(removed.decode())
    assert remove_calls == []
    assert removed_cfg["genie_space_id_to_name"] == {CAPTURE_IDS["Alpha"]: "Alpha2"}
    assert fake.generate()[0] == removed


def test_transient_api_failure_keeps_reviewed_inline_agent_config(env_dir, monkeypatch):
    env_file = env_dir / "env.auto.tfvars"
    named = ENV.replace(
        f'genie_spaces = [{{ genie_space_id = "{SPACE_ID}",',
        f'genie_spaces = [{{ name = "{AGENT}", genie_space_id = "{SPACE_ID}",',
    )
    env_file.write_text(named)
    _generate(env_dir, monkeypatch)
    tfvars = env_dir / "generated/abac.auto.tfvars"
    first = tfvars.read_bytes()
    (env_dir / "generated/governed_tables.json").write_text(
        json.dumps({"tables": TABLES}) + "\n"
    )

    env_file.write_text(named.replace(
        f'acl_groups = {json.dumps(TIERS)}',
        f'uc_tables = {json.dumps(TABLES)}, acl_groups = {json.dumps(TIERS)}',
    ))
    monkeypatch.setattr(
        generate_abac, "fetch_tables_from_genie_space",
        lambda *args, **kwargs: ([], {}, "", False),
    )
    _generate(env_dir, monkeypatch)

    assert tfvars.read_bytes() == first
    assert hcl2.loads(tfvars.read_text())["genie_space_configs"] == {
        AGENT: {"instructions": "Amounts are in AUD."}
    }


def test_recapturing_agent_content_does_not_change_generated(tmp_path, monkeypatch):
    fake = _CaptureGenerateEnv(tmp_path, monkeypatch)
    fake.add("Alpha")
    first, first_calls = fake.generate()
    fake.capture("Alpha")
    second, second_calls = fake.generate()

    capture = fake.env / "agents/Alpha.space.json"
    payload = json.loads(capture.read_text())
    payload["instructions"] = {
        "text_instructions": [{"content": ["Recaptured instr."]}]
    }
    capture.write_text(json.dumps(payload) + "\n")
    third, third_calls = fake.generate()

    assert first_calls == [CAPTURE_IDS["Alpha"]]
    assert second_calls == third_calls == []
    assert first == second == third
    assert hcl2.loads(third.decode())["genie_space_configs"] == {"Alpha": {}}


def test_generate_refuses_two_agent_files_with_the_same_id(tmp_path, monkeypatch, capsys):
    fake = _CaptureGenerateEnv(tmp_path, monkeypatch)
    fake.add("Alpha")
    duplicate = fake.env / "agents/Duplicate.auto.tfvars"
    duplicate.write_text(
        f'genie_space_id = "{CAPTURE_IDS["Alpha"]}"\n'
        f'acl_groups = {json.dumps(TIERS)}\n'
    )
    monkeypatch.setattr(sys, "argv", [
        "generate_abac.py", "--auth-file", str(fake.env / "auth.auto.tfvars"),
    ])

    assert generate_abac.main() == 1
    assert "share genie_space_id" in capsys.readouterr().out


def test_a_free_text_column_with_several_partial_classes_is_redacted_and_marked(env_dir, monkeypatch):
    ddl = DDL.replace("  date_of_birth DATE\n);", "  date_of_birth DATE,\n  notes STRING\n);")
    rows = TAG_ROWS + [("dev_fin", "payments", "customers", "notes", f"class.{name}", "")
                       for name in ("email_address", "name", "phone_number")]
    monkeypatch.setattr(generate_abac, "fetch_tables_from_databricks",
                        lambda refs, cfg: (ddl, [("dev_fin", "payments")]))
    monkeypatch.setattr(generate_abac, "_fetch_live_classification_source",
                        lambda *a, **k: ClassificationSource(tag_rows=rows))
    _reviewed_draft(env_dir)

    _generate(env_dir, monkeypatch)

    tfvars = env_dir / "generated/abac.auto.tfvars"
    first = tfvars.read_text()
    values = {a["entity_name"]: a["tag_value"] for a in _treatments(hcl2.loads(first))}
    assert values["dev_fin.payments.customers.notes"].startswith("redact__string__")
    assert values["dev_fin.payments.customers.email"].startswith("email_partial__string__")
    marker = ("# gr.classification_mixed_partial: dev_fin.payments.customers.notes"
              "|email_partial,name_partial,phone_partial")
    assert first.splitlines().count(marker) == 1
    assert "classification_mixed_partial" not in first.replace(marker, "")

    _generate(env_dir, monkeypatch)
    assert tfvars.read_text() == first


def test_dry_run_builds_no_prompt(env_dir, monkeypatch, capfd):
    with pytest.raises(SystemExit) as exc:
        _generate(env_dir, monkeypatch, "--dry-run")
    assert exc.value.code == 0
    assert "calls no model" in capfd.readouterr().out
    assert not (env_dir / "generated").exists()


@pytest.mark.parametrize("args, message", [
    (["--create-groups"], "--create-groups needs a model"),
    (["--space", AGENT], "derives the whole governed set at once"),
    (["--tables", "dev_fin.payments.customers"], "derives the whole governed set at once"),
])
def test_unsupported_options_refuse_before_any_fetch(env_dir, monkeypatch, capfd, args, message):
    monkeypatch.setattr(generate_abac, "fetch_tables_from_databricks",
                        lambda *a, **k: pytest.fail("refused before any table fetch"))
    with pytest.raises(SystemExit) as exc:
        _generate(env_dir, monkeypatch, *args)
    assert exc.value.code == 1
    assert message in capfd.readouterr().out


def test_missing_class_tags_refuse_before_writing(env_dir, monkeypatch, capfd):
    (env_dir / "env.auto.tfvars").write_text(ENV.replace("enable_classification = true\n", ""))
    monkeypatch.setattr(generate_abac, "_fetch_live_classification_source", lambda *a, **k: None)
    with pytest.raises(SystemExit) as exc:
        _generate(env_dir, monkeypatch)
    assert exc.value.code == 1
    assert "computed from native class.* tags" in capfd.readouterr().out
    assert not (env_dir / "generated/abac.auto.tfvars").exists()


def test_genie_mode_imports_genie_config_only(env_dir, monkeypatch):
    _generate(env_dir, monkeypatch, "--mode", "genie")
    cfg = hcl2.loads((env_dir / "generated/abac.auto.tfvars").read_text())
    assert cfg["genie_space_configs"][AGENT]["instructions"] == "Amounts are in AUD."
    assert not cfg.get("fgac_policies") and not cfg.get("groups")
    assert not (env_dir / "generated/masking_functions.sql").exists()


def test_legacy_generate_still_calls_the_model(env_dir, monkeypatch):
    (env_dir / "env.auto.tfvars").write_text(ENV.replace('governance_mode = "deterministic"\n', ""))
    calls = []

    def model(*_a, **_k):
        calls.append(1)
        raise SystemExit(7)

    monkeypatch.setattr(generate_abac, "configure_databricks_env", lambda cfg: None)
    monkeypatch.setattr(generate_abac, "build_prompt", lambda *a, **k: "prompt")
    monkeypatch.setattr(generate_abac, "call_with_retries", model)
    with pytest.raises(SystemExit) as exc:
        _generate(env_dir, monkeypatch)
    assert exc.value.code == 7 and calls == [1]



def test_fresh_env_agent_without_acl_groups_stops_cleanly(env_dir, monkeypatch, capfd):
    # Decision 4: with no model there are no drafted policies to derive CAN_RUN
    # from, so an agent without acl_groups stops the run and leaves no draft.
    (env_dir / "env.auto.tfvars").write_text(
        ENV.replace(f", acl_groups = {json.dumps(TIERS)}", ""))
    with pytest.raises(SystemExit) as exc:
        _generate(env_dir, monkeypatch)
    assert exc.value.code == 1
    assert "ERROR: Cannot derive ACL for Genie space 'Payments Agent'" in capfd.readouterr().out
    assert not (env_dir / "generated/abac.auto.tfvars").exists()


def test_opted_in_legacy_env_refuses_until_rebuild_governed_tables(env_dir, monkeypatch, capfd):
    # A legacy env's draft has treatment tags but no governed_tables.json.
    generated = env_dir / "generated"
    generated.mkdir()
    (generated / "abac.auto.tfvars").write_text(REVIEWED)
    with pytest.raises(SystemExit) as exc:
        _generate(env_dir, monkeypatch)
    assert exc.value.code == 1
    out = capfd.readouterr().out
    assert (f"ERROR: {generated / 'governed_tables.json'} is missing but this env has recorded governance "
            "(its generated or promoted config has treatment tags); "
            "rebuild it with: make rebuild-governed-tables ENV=dev") in out
    assert (generated / "abac.auto.tfvars").read_text() == REVIEWED


def test_migration_doc_runs_rebuild_before_the_first_deterministic_generate():
    doc = (Path(generate_abac.__file__).parent / "docs/legacy-migration.md").read_text()
    steps = doc[doc.index("```bash"):doc.index("```", doc.index("```bash") + 3)]
    assert steps.index("make rebuild-governed-tables ENV=dev") < steps.index("make generate ENV=dev")
    assert steps.index("make rebuild-governed-tables ENV=prod") < steps.index("make promote-to ENV=prod")
    assert steps.index("make promote-to ENV=prod") < steps.rindex("make migrate-plan ENV=prod")
    assert "rebuild it with: make rebuild-governed-tables ENV=dev" in doc


def _env_file_as(env_dir, case):
    env = env_dir / "env.auto.tfvars"
    if case in ("full", "minimal"):
        env.write_text(ENV if case == "full" else 'governance_mode = "deterministic"\n')
        return "a deterministic env never calls one"
    env.unlink()
    if case == "directory":
        env.mkdir()
    elif case == "symlink":
        (env_dir / "real.tfvars").write_text('governance_mode = "legacy"\n')
        env.symlink_to("real.tfvars")
    elif case == "unparsable":
        env.write_text("governance_mode = = {\n")
        return "cannot parse"
    return "is missing or not a regular file"


# Missing, non-file and unparsable env files fail closed: never into legacy delta.
@pytest.mark.parametrize("case", ["full", "minimal", "missing", "directory", "symlink", "unparsable"])
def test_delta_refuses_before_any_sdk_model_or_write(env_dir, monkeypatch, capfd, case):
    message = _env_file_as(env_dir, case)
    generated = _reviewed_draft(env_dir)
    before = {p: p.read_bytes() for p in env_dir.rglob("*") if p.is_file()}
    monkeypatch.setattr(generate_abac, "_run_delta_mode",
                        lambda *a, **k: pytest.fail("the delta path must not start"))
    import scripts.audit_schema_drift as drift
    for name in ("_get_sdk_client", "_get_warehouse_id", "detect_forward_drift", "detect_reverse_drift"):
        monkeypatch.setattr(drift, name, lambda *a, **k: pytest.fail("no SDK call"), raising=False)
    for name in ("write_text", "write_bytes", "unlink", "mkdir"):
        monkeypatch.setattr(Path, name, lambda *a, **k: pytest.fail("no write"))
    with pytest.raises(SystemExit) as exc:
        _generate(env_dir, monkeypatch, "--delta")
    assert exc.value.code == 2
    err = capfd.readouterr().err
    assert message in err
    if case in ("full", "minimal"):
        assert "make generate" in err and "make derive-assignments" in err
    monkeypatch.undo()
    assert {p: p.read_bytes() for p in env_dir.rglob("*") if p.is_file()} == before
    assert generated.is_dir()


def test_legacy_delta_still_dispatches(env_dir, monkeypatch):
    (env_dir / "env.auto.tfvars").write_text(ENV.replace('governance_mode = "deterministic"\n', ""))
    calls = []
    monkeypatch.setattr(generate_abac, "_run_delta_mode", lambda auth_file: calls.append(auth_file))
    _generate(env_dir, monkeypatch, "--delta")
    assert calls == [env_dir / "auth.auto.tfvars"]
