"""Golden output of generate_abac.main() for the generate_abac.py split (18.7).

Each case runs main() offline on a fixed env and compares every file it leaves
behind, its exit code and its output with a golden captured from main before
any code moved. A split that changes one byte of output fails here.

Regenerate (only on a commit whose output is the reference):
    GENERATE_ABAC_GOLDEN_UPDATE=1 pytest shared/tests/test_generate_abac_golden.py
"""

import json
import os
import socket
import sys
from pathlib import Path

import pytest

import generate_abac
from sensitivity_source import ClassificationSource

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "generate_abac_golden"
UPDATE = os.environ.get("GENERATE_ABAC_GOLDEN_UPDATE") == "1"

SPACE_ID = "01ef7b3c2a4d5e6f"
AGENT = "Payments Agent"
TABLES = ["dev_fin.payments.customers", "dev_fin.payments.payments"]
TIERS = ["payments_ops", "regional_analysts", "viewers"]
DEPLOYER = "82bb9d6d-0000-0000-0000-000000000000"
SERIALIZED = json.dumps({
    "data_sources": {"tables": [{"identifier": table} for table in TABLES]},
    "instructions": {"text_instructions": [{"content": ["Amounts are in AUD."]}]},
    "config": {"sample_questions": [{"question": ["Total payments by month?"]}]},
})
DDL = """-- Table: dev_fin.payments.customers
CREATE TABLE dev_fin.payments.customers (
  customer_id BIGINT,
  email STRING,
  phone STRING,
  ssn STRING,
  date_of_birth DATE
);

-- Table: dev_fin.payments.payments
CREATE TABLE dev_fin.payments.payments (
  payment_id BIGINT,
  spend_limit DECIMAL(18,2),
  cvv STRING
);"""
TAG_ROWS = [
    ("dev_fin", "payments", "customers", "email", "class.email_address", ""),
    ("dev_fin", "payments", "customers", "ssn", "class.us_ssn", ""),
    ("dev_fin", "payments", "customers", "date_of_birth", "class.date_of_birth", ""),
    ("dev_fin", "payments", "payments", "cvv", "class.card_security_code", ""),
]
AUTH = (
    'databricks_workspace_host = "https://example.invalid"\n'
    f'databricks_client_id = "{DEPLOYER}"\ndatabricks_client_secret = "secret"\n'
)
LEGACY_ENV = f'genie_spaces = [{{ genie_space_id = "{SPACE_ID}" }}]\n'
DETERMINISTIC_ENV = f'''governance_mode = "deterministic"
deployer_principal_id = "{DEPLOYER}"
enable_classification = true
access_tier_groups = {json.dumps(TIERS)}
genie_spaces = [{{ genie_space_id = "{SPACE_ID}", acl_groups = {json.dumps(TIERS)} }}]
'''

MODEL_SQL = """```sql
USE CATALOG dev_fin;
USE SCHEMA payments;

CREATE OR REPLACE FUNCTION mask_email(email STRING)
RETURNS STRING
RETURN CASE WHEN email IS NULL THEN NULL ELSE CONCAT('***@', SPLIT(email, '@')[1]) END;

CREATE OR REPLACE FUNCTION mask_amount_rounded(amount DECIMAL(18,2))
RETURNS DECIMAL(18,2)
RETURN ROUND(amount, -2);

CREATE OR REPLACE FUNCTION mask_redact(val STRING)
RETURNS STRING
RETURN '[REDACTED]';
```"""
# A model draft with the defects the text/HCL helpers repair: auth keys and a
# SQL comment to strip, a missing comma between objects, a stray comma line,
# an object-style tag value and a legacy single-space Genie key.
MODEL_HCL = """```hcl
# Authentication
databricks_account_id = "acct"
uc_tables = ["dev_fin.payments.customers"]
-- the model's SQL habit
groups = {
  "payments_ops" = { description = "Full access" }
  "viewers" = { description = "Masked" }
}

tag_policies = [
  { key = "pii_level", description = "PII", values = ["masked_email", "redacted"] },
  { key = "financial_sensitivity", description = "Fin", values = [{ name = "rounded_amounts" }] },
]

tag_assignments = [
  { entity_type = "columns", entity_name = "dev_fin.payments.customers.email", tag_key = "pii_level", tag_value = "masked_email" },
  { entity_type = "columns", entity_name = "dev_fin.payments.customers.ssn", tag_key = "pii_level", tag_value = "redacted" },
  ,
  { entity_type = "columns", entity_name = "dev_fin.payments.payments.spend_limit", tag_key = "financial_sensitivity", tag_value = "rounded_amounts" },
]

fgac_policies = [
  {
    name = "mask_email"
    policy_type = "POLICY_TYPE_COLUMN_MASK"
    catalog = "dev_fin"
    to_principals = ["viewers"]
    match_condition = "hasTagValue('pii_level', 'masked_email')"
    match_alias = "cols"
    function_name = "mask_email"
    function_catalog = "dev_fin"
    function_schema = "payments"
  }
  {
    name = "mask_ssn"
    policy_type = "POLICY_TYPE_COLUMN_MASK"
    catalog = "dev_fin"
    to_principals = ["viewers"]
    match_condition = "hasTagValue('pii_level', 'redacted')"
    match_alias = "cols"
    function_name = "mask_redact"
    function_catalog = "dev_fin"
    function_schema = "payments"
  },
  {
    name = "mask_spend_limit"
    policy_type = "POLICY_TYPE_COLUMN_MASK"
    catalog = "dev_fin"
    to_principals = ["viewers"]
    match_condition = "hasTagValue('financial_sensitivity', 'rounded_amounts')"
    match_alias = "cols"
    function_name = "mask_amount_rounded"
    function_catalog = "dev_fin"
    function_schema = "payments"
  },
]

genie_space_configs = {
  "Payments Agent" = {
    title = "Payments Agent"
    description = "Payments analytics"
    sample_questions = ["Which customers paid most?"]
    instructions = "Model instructions."
    benchmarks = [
      { question = "How many payments?", sql = "SELECT COUNT(*) FROM dev_fin.payments.payments" },
    ]
  }
}

genie_space_title = "legacy single-space key"
```"""
MODEL_RESPONSE = MODEL_SQL + "\n\n" + MODEL_HCL

# A reviewed deterministic draft: its masks are replaced, its row filter, table
# tag, group description and functions are kept.
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
genie_space_configs = {
  "Payments Agent" = {
    title = "Payments Agent"
    instructions = "Reviewed: amounts are in AUD, never USD."
  }
}
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

LEGACY_ARGS = ["--groups", "payments_ops,viewers"]
# case name -> (env text, runs of extra CLI args, reviewed draft to start from)
CASES = {
    "legacy_full": (LEGACY_ENV, [LEGACY_ARGS], False),
    "legacy_governance": (LEGACY_ENV, [LEGACY_ARGS + ["--mode", "governance"]], False),
    "legacy_genie": (LEGACY_ENV, [LEGACY_ARGS + ["--mode", "genie"]], False),
    # The second run starts from the first run's draft (reviewed Genie text).
    "legacy_rerun": (LEGACY_ENV, [LEGACY_ARGS, LEGACY_ARGS + ["--allow-rule-changes"]], False),
    "deterministic_full": (DETERMINISTIC_ENV, [[]], False),
    "deterministic_governance": (DETERMINISTIC_ENV, [["--mode", "governance"]], False),
    "deterministic_genie": (DETERMINISTIC_ENV, [["--mode", "genie"]], False),
    "deterministic_rerun": (DETERMINISTIC_ENV, [[], []], True),
}


def _offline(monkeypatch, env_dir):
    """Stub every Databricks, model and network boundary main() reaches."""
    for name in ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_CLIENT_ID",
                 "DATABRICKS_CLIENT_SECRET", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                 "VERBOSE", "SPACE", "MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(env_dir)
    monkeypatch.setattr(generate_abac, "WORK_DIR", env_dir)
    monkeypatch.setattr(generate_abac, "configure_databricks_env", lambda cfg: None)
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
    monkeypatch.setattr(generate_abac, "_fetch_live_tag_policy_values", lambda: {})
    monkeypatch.setattr(generate_abac, "_LIVE_POLICIES_REPORTED", False)
    # Module state an earlier test in the same process may have filled.
    monkeypatch.setattr(generate_abac, "_POLICY_AUTOFIXES", set())
    monkeypatch.setattr(generate_abac, "call_with_retries", lambda *a, **k: MODEL_RESPONSE)

    def no_network(*_a, **_k):
        raise AssertionError("the golden run must not reach the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)


def _snapshot(root: Path) -> list[tuple[str, bytes]]:
    return [(path.relative_to(root).as_posix(), path.read_bytes())
            for path in sorted(root.rglob("*")) if path.is_file()]


def _render(files, exits, output) -> bytes:
    """One golden file: a header per file with its byte length, then its bytes."""
    parts = [f"### EXIT {' '.join(map(str, exits))}\n".encode()]
    parts.append(f"### OUTPUT {len(output)}\n".encode() + output + b"\n")
    for rel, data in files:
        parts.append(f"### FILE {rel} {len(data)}\n".encode() + data + b"\n")
    return b"".join(parts)


def _run_case(name, tmp_path, monkeypatch, capfd) -> bytes:
    env_text, runs, reviewed = CASES[name]
    env_dir = tmp_path / "env"
    (env_dir / "data_access").mkdir(parents=True)
    (env_dir / "env.auto.tfvars").write_text(env_text)
    (env_dir / "auth.auto.tfvars").write_text(AUTH)
    if reviewed:
        generated = env_dir / "generated"
        generated.mkdir()
        (generated / "abac.auto.tfvars").write_text(REVIEWED)
        (generated / "masking_functions.sql").write_text(REVIEWED_SQL)
        (generated / "governed_tables.json").write_text(json.dumps({"tables": TABLES}) + "\n")
    _offline(monkeypatch, env_dir)
    capfd.readouterr()
    exits = []
    for extra in runs:
        monkeypatch.setattr(sys, "argv", [
            "generate_abac.py", "--auth-file", str(env_dir / "auth.auto.tfvars"), *extra,
        ])
        try:
            generate_abac.main()
            exits.append(0)
        except SystemExit as exc:
            exits.append(exc.code if isinstance(exc.code, int) else 1)
        sys.stdout.flush()
    out = capfd.readouterr()
    output = (out.out + out.err).encode()
    for root in {str(tmp_path), os.path.realpath(tmp_path)}:
        output = output.replace(root.encode(), b"<TMP>")
    files = [(rel, data.replace(str(tmp_path).encode(), b"<TMP>")
              .replace(os.path.realpath(tmp_path).encode(), b"<TMP>"))
             for rel, data in _snapshot(env_dir)]
    return _render(files, exits, output)


@pytest.mark.parametrize("name", sorted(CASES))
def test_generate_output_matches_the_golden(name, tmp_path, monkeypatch, capfd):
    actual = _run_case(name, tmp_path, monkeypatch, capfd)
    golden = GOLDEN_DIR / f"{name}.golden"
    if UPDATE:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        golden.write_bytes(actual)
    assert golden.exists(), f"{golden} is missing; capture it with GENERATE_ABAC_GOLDEN_UPDATE=1"
    expected = golden.read_bytes()
    if actual != expected:
        # Show the first differing line rather than two multi-KB blobs.
        for number, (want, got) in enumerate(zip(expected.splitlines(), actual.splitlines()), 1):
            if want != got:
                pytest.fail(f"{name}: line {number} differs from the golden\n"
                            f"  golden: {want!r}\n  actual: {got!r}")
        pytest.fail(f"{name}: output length differs from the golden "
                    f"({len(actual)} bytes, golden {len(expected)})")


def test_the_cases_cover_every_mode_and_draft_path():
    """The goldens span legacy and deterministic, and full, governance and genie."""
    modes = {next((a[i + 1] for i, x in enumerate(a) if x == "--mode"), "full")
             for _env, runs, _reviewed in CASES.values() for a in runs}
    assert modes == {"full", "governance", "genie"}
    assert {"legacy", "deterministic"} == {name.split("_")[0] for name in CASES}
    for name in CASES:
        text = (GOLDEN_DIR / f"{name}.golden").read_text()
        assert "### FILE generated/abac.auto.tfvars " in text, name
        assert text.startswith("### EXIT 0"), name
