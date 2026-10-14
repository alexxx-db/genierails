"""make check, make promote and make promote-to on deterministic output (real make).

The live AWS Phase A run found three gaps that only show with real
deterministic output: the deployer SP in every EXCEPT list, gr_treatment values
that aren't in the tag registry, and a prod that already has an agent:

- check validates in a scratch copy without credentials, so it refused the
  deployer SP that plain make validate accepts from auth.auto.tfvars (N2);
- the split refused deterministic tag values, so promote, promote-to and
  check ENV=prod crashed (N3);
- promote-to kept prod's existing agent without acl_groups, which a
  deterministic env refuses (N4).

Terraform is a stub on PATH; nothing reaches Databricks.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import hcl2
import pytest

import generate_abac
from governance_policies import build_model, column_types_from_ddl
from tests.test_check_target import FAKE_TERRAFORM, GITIGNORE, _env, _git, _snapshot

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "shared"

if shutil.which("make") is None or shutil.which("git") is None:
    pytest.skip("make and git are required", allow_module_level=True)

# One deployer SP for both envs and the same group names, like the live run
# (the tests at the end give prod its own SP and its own group names).
DEPLOYER = "82bb9d6d-0000-0000-0000-000000000001"
TIERS = ["dev_ops", "dev_analysts"]
SPACE = "Payments"
DEV_SPACE_ID = "01ef00000000000a"
DDL = """CREATE TABLE paycat.s.t (
  id BIGINT,
  card STRING,
  email STRING
);"""
CLASSES = {"paycat.s.t.card": ["class.credit_card"], "paycat.s.t.email": ["class.email_address"]}
DEV_ENV = f'''governance_mode = "deterministic"
access_tier_groups = {json.dumps(TIERS)}
table_readers = {{ "paycat.s.t" = {json.dumps(TIERS)} }}
genie_spaces = [
  {{ name = "{SPACE}", genie_space_id = "{DEV_SPACE_ID}", uc_tables = ["paycat.s.t"], acl_groups = {json.dumps(TIERS)} }},
]
'''
# What the existing (legacy) account layer already holds in a live env.
LEGACY_ACCOUNT = '''tag_policies = [
  { key = "gr_treatment", description = "legacy", values = ["account_last4", "email_partial"] },
]
'''
# Prod opted in by hand; its agent already exists and, from legacy, has no acl_groups.
PROD_ENV = '''governance_mode = "deterministic"
promote_from = "dev"
catalog_map = { "paycat" = "ppay" }
genie_spaces = [
  { name = "Payments", genie_space_id = "", uc_tables = ["ppay.s.t"] },
]
'''


def _auth(client_id, host="https://unreachable.invalid"):
    # What the pr-check workflow writes: the deployer's application ID, no secret.
    return (f'databricks_account_id = ""\ndatabricks_client_id = "{client_id}"\n'
            'databricks_client_secret = ""\ndatabricks_workspace_id = "1"\n'
            f'databricks_workspace_host = "{host}"\n')


def _make(repo, tmp_path, *args, **env):
    return subprocess.run(["make", "--no-print-directory", "-C", str(repo / "aws"), *args],
                          text=True, capture_output=True, env=_env(tmp_path, **env), timeout=600)


def _envs(repo):
    return repo / "aws" / "envs"


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _write_generated(dev):
    """dev's deterministic generate output, built the way generate builds it."""
    settings = hcl2.loads(DEV_ENV)
    model = build_model(CLASSES, column_types_from_ddl(DDL), settings, deployer_principal=DEPLOYER)
    generated = dev / "generated"
    generated.mkdir(parents=True)
    tfvars = generated / "abac.auto.tfvars"
    groups = "\n".join(f'  "{group}" = {{ description = "tier" }}' for group in TIERS)
    tfvars.write_text(f'groups = {{\n{groups}\n}}\n'
                      f'genie_space_configs = {{\n  "{SPACE}" = {{ title = "{SPACE}" }}\n}}\n'
                      f'genie_space_id_to_name = {{ "{DEV_SPACE_ID}" = "{SPACE}" }}\n')
    sql = generated / "masking_functions.sql"
    sql.write_text("USE CATALOG paycat;\nUSE SCHEMA s;\n")
    generate_abac.write_deterministic_model(tfvars, sql, model)
    (dev / "ddl").mkdir(exist_ok=True)
    return model


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    dev = _envs(repo) / "dev"
    dev.mkdir(parents=True)
    (_envs(repo) / "account").mkdir()
    shutil.copy2(ROOT / "aws" / "Makefile", repo / "aws" / "Makefile")
    (repo / "shared").symlink_to(SHARED)
    (dev / "env.auto.tfvars").write_text(DEV_ENV)
    (dev / "auth.auto.tfvars").write_text(_auth(DEPLOYER))
    (_envs(repo) / "account" / "abac.auto.tfvars").write_text(LEGACY_ACCOUNT)
    model = _write_generated(dev)
    assert any(DEPLOYER in (p.get("except_principals") or []) for p in model.fgac_policies)
    (repo / ".gitignore").write_text(GITIGNORE)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "terraform").write_text(FAKE_TERRAFORM)
    (bin_dir / "terraform").chmod(0o755)
    result = _make(repo, tmp_path, "promote", "ENV=dev")
    assert result.returncode == 0, result.stdout + result.stderr
    subprocess.run([sys.executable, str(SHARED / "scripts" / "sticky_governance.py"), "record",
                    "--env-dir", str(dev), "--config", str(dev / "generated" / "abac.auto.tfvars")], check=True)
    _git(repo, "init", "-q")
    _commit(repo, "dev")
    return repo


def _promote_to_prod(repo, tmp_path, env_text=PROD_ENV, deployer=DEPLOYER):
    prod = _envs(repo) / "prod"
    prod.mkdir()
    (prod / "env.auto.tfvars").write_text(env_text)
    # No host: check's plan step (6) reads prod's classification config live
    # (promote-to turns classification on), which fails fast without one.
    (prod / "auth.auto.tfvars").write_text(_auth(deployer, host=""))
    result = _make(repo, tmp_path, "promote-to", "ENV=prod")
    assert result.returncode == 0, result.stdout + result.stderr
    (tmp_path / "promote.log").write_text(result.stdout + result.stderr)
    subprocess.run([sys.executable, str(SHARED / "scripts" / "sticky_governance.py"), "record",
                    "--env-dir", str(prod), "--config", str(prod / "generated" / "abac.auto.tfvars")], check=True)
    _commit(repo, "promote")
    return repo


@pytest.fixture
def promoted(repo, tmp_path):
    return _promote_to_prod(repo, tmp_path)


def _state(repo):
    return _snapshot(_envs(repo)), _git(repo, "status", "--porcelain", "--ignored", "--untracked-files=all")


# ── N3: the split takes deterministic tag values next to legacy ones ────────

def test_same_env_promote_merges_deterministic_values_into_the_legacy_account_policy(repo):
    account = hcl2.loads((_envs(repo) / "account" / "abac.auto.tfvars").read_text())
    values = {p["key"]: p["values"] for p in account["tag_policies"]}["gr_treatment"]
    generated = hcl2.loads((_envs(repo) / "dev" / "generated" / "abac.auto.tfvars").read_text())
    deterministic = sorted({a["tag_value"] for a in generated["tag_assignments"]})
    assert len(deterministic) == 2 and all("__" in value for value in deterministic)
    assert values[:2] == ["account_last4", "email_partial"]
    assert sorted(values[2:]) == deterministic


# ── N2: check validates the deployer SP the way plain validate does ─────────

@pytest.mark.parametrize("ci", ["", "true"])
def test_check_accepts_the_deployer_sp_without_credentials_and_writes_nothing(repo, tmp_path, ci):
    assert _make(repo, tmp_path, "validate", "ENV=dev").returncode == 0
    before = _state(repo)

    result = _make(repo, tmp_path, "check", "ENV=dev", **({"CI": ci} if ci else {}))

    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "not defined in 'groups'" not in out
    assert "OK: generated files (envs/dev): committed files match." in out
    assert "=== make check ENV=dev: passed ===" in out
    assert _state(repo) == before


def test_check_still_refuses_an_unknown_except_principal(repo, tmp_path):
    for rel in ("generated/abac.auto.tfvars", "data_access/abac.auto.tfvars"):
        path = _envs(repo) / "dev" / rel
        path.write_text(path.read_text().replace(DEPLOYER, "stranger-sp"))
    _commit(repo, "unknown principal")

    result = _make(repo, tmp_path, "check", "ENV=dev", CI="true")

    assert result.returncode != 0
    assert "except_principals group 'stranger-sp' not defined in 'groups'" in result.stdout + result.stderr


def test_check_refuses_the_deployer_sp_when_no_auth_names_it(repo, tmp_path):
    (_envs(repo) / "dev" / "auth.auto.tfvars").unlink()

    result = _make(repo, tmp_path, "check", "ENV=dev", CI="true")

    assert result.returncode != 0
    assert f"except_principals group '{DEPLOYER}' not defined in 'groups'" in result.stdout + result.stderr


def test_check_copy_gets_only_the_deployer_id_never_the_secret(tmp_path):
    sys.path.insert(0, str(SHARED / "scripts"))
    from check_env import Scratch

    cloud = tmp_path / "aws"
    for env in ("dev", "prod", "account"):
        (cloud / "envs" / env).mkdir(parents=True)
    (cloud / "envs" / "dev" / "auth.auto.tfvars").write_text(_auth(DEPLOYER).replace(
        'databricks_client_secret = ""', 'databricks_client_secret = "do-not-copy"'))
    (cloud / "envs" / "account" / "auth.auto.tfvars").write_text(_auth("account-sp"))
    shutil.copy2(ROOT / "aws" / "Makefile", cloud / "Makefile")
    scratch = Scratch(cloud, cloud / "Makefile", "make")
    try:
        copied = scratch.envs / "dev" / "auth.auto.tfvars"
        auth = hcl2.loads(copied.read_text())
        assert auth["databricks_client_id"] == DEPLOYER
        assert {k: v for k, v in auth.items() if k != "databricks_client_id"} == {
            "databricks_account_id": "", "databricks_client_secret": "",
            "databricks_workspace_id": "", "databricks_workspace_host": ""}
        assert oct(copied.stat().st_mode & 0o777) == "0o600"
        assert not any("do-not-copy" in p.read_text() for p in scratch.root.rglob("*") if p.is_file())
        # prod has no auth file; the account's stays out (the scaffold links it).
        assert not (scratch.envs / "prod" / "auth.auto.tfvars").exists()
        assert not (scratch.envs / "account" / "auth.auto.tfvars").exists()
    finally:
        scratch.close()


# ── N3 + N4: promote-to into a prod that opted in by hand ───────────────────

def test_promote_to_seeds_the_missing_acl_groups(promoted, tmp_path):
    prod = hcl2.loads((_envs(promoted) / "prod" / "env.auto.tfvars").read_text())

    assert prod["governance_mode"] == "deterministic"
    assert prod["genie_spaces"][0]["acl_groups"] == TIERS
    assert prod["access_tier_groups"] == TIERS
    assert f"Seeded destination Genie space 'Payments' acl_groups={TIERS!r}" in (tmp_path / "promote.log").read_text()


def test_promote_to_keeps_acl_groups_prod_already_has(promoted, tmp_path):
    env_file = _envs(promoted) / "prod" / "env.auto.tfvars"
    env_file.write_text(env_file.read_text().replace('acl_groups       = ["dev_ops", "dev_analysts"]',
                                                     'acl_groups       = ["dev_ops"]'))
    _commit(promoted, "prod narrows the agent")

    result = _make(promoted, tmp_path, "promote-to", "ENV=prod")

    assert result.returncode == 0, result.stdout + result.stderr
    prod = hcl2.loads(env_file.read_text())
    assert prod["genie_spaces"][0]["acl_groups"] == ["dev_ops"]
    assert "Preserved destination Genie space 'Payments' acl_groups=['dev_ops']" in result.stdout


def _assert_check_and_promote_check_pass(repo, tmp_path, ci):
    before = _state(repo)
    extra = {"CI": ci} if ci else {}

    check = _make(repo, tmp_path, "check", "ENV=prod", **extra)
    promote_check = _make(repo, tmp_path, "promote-to", "ENV=prod", "CHECK=1", **extra)

    out = check.stdout + check.stderr
    # Steps 1-5 pass offline; the plan (6) needs prod's live classification config.
    assert "OK: promote-to ENV=prod CHECK=1: committed files match." in out, out
    assert "OK: generated files (envs/prod): committed files match." in out, out
    assert "--- check 5/6: mask-library unit tests ---" in out and "--- check 6/6: plan ---" in out, out
    assert "not defined in 'groups'" not in out and "unknown canonical value" not in out
    assert check.returncode == 0 or "=== make check ENV=prod: FAILED (plan) ===" in out, out
    assert promote_check.returncode == 0, promote_check.stdout + promote_check.stderr
    assert "OK: promote-to ENV=prod CHECK=1: committed files match." in promote_check.stdout
    assert _state(repo) == before


@pytest.mark.parametrize("ci", ["", "true"])
def test_check_prod_and_promote_check_pass_on_the_promotion(promoted, tmp_path, ci):
    _assert_check_and_promote_check_pass(promoted, tmp_path, ci)


def _policies(env_dir):
    return {rel: hcl2.loads((env_dir / rel).read_text())["fgac_policies"]
            for rel in ("generated/abac.auto.tfvars", "data_access/abac.auto.tfvars")}


# ── Each env applies with its own deployer SP ───────────────────────────────

PROD_DEPLOYER = "5a7e0000-0000-0000-0000-00000000beef"


@pytest.mark.parametrize("ci", ["", "true"])
def test_promote_to_swaps_dev_deployer_for_prods_and_check_passes(repo, tmp_path, ci):
    dev_policies = {p["name"]: p.get("except_principals") or []
                    for p in hcl2.loads((_envs(repo) / "dev" / "generated" / "abac.auto.tfvars").read_text())["fgac_policies"]}
    assert any(DEPLOYER in names for names in dev_policies.values())

    _promote_to_prod(repo, tmp_path, deployer=PROD_DEPLOYER)

    for rel, policies in _policies(_envs(repo) / "prod").items():
        assert len(policies) == len(dev_policies), rel
        for policy, dev_except in zip(policies, dev_policies.values()):
            # Only the deployer changes, in place; a list without it gets none.
            expected = [PROD_DEPLOYER if name == DEPLOYER else name for name in dev_except]
            assert (policy.get("except_principals") or []) == expected, (rel, policy["name"])
            assert PROD_DEPLOYER not in (policy.get("to_principals") or []), (rel, policy["name"])
    for path in (_envs(repo) / "prod").rglob("*"):
        if path.is_file() and path.name != "auth.auto.tfvars":
            assert DEPLOYER not in path.read_text(), path
    _assert_check_and_promote_check_pass(repo, tmp_path, ci)


def test_promote_to_refuses_without_a_destination_deployer_id(repo, tmp_path):
    prod = _envs(repo) / "prod"
    prod.mkdir()
    (prod / "env.auto.tfvars").write_text(PROD_ENV)
    (prod / "auth.auto.tfvars").write_text(_auth("", host=""))

    result = _make(repo, tmp_path, "promote-to", "ENV=prod")

    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "no destination deployer ID is set" in out and "nothing was written" in out, out
    assert not (prod / "generated" / "abac.auto.tfvars").exists()
    assert not (prod / "data_access" / "abac.auto.tfvars").exists()


def test_promote_to_never_adds_the_deployer_to_a_never_raw_policy(tmp_path):
    sys.path.insert(0, str(SHARED / "scripts"))
    from remap_generated_config import map_principals

    text = ('fgac_policies = [\n  {\n    name = "raw"\n    except_principals = ["t1", "dev-sp"]\n  },\n'
            '  {\n    name = "never"\n    except_principals = []\n    to_principals = ["dev-sp"]\n  },\n]\n')

    out = hcl2.loads(map_principals(text, {}, set(), [], ("dev-sp", "prod-sp")))["fgac_policies"]

    assert out[0]["except_principals"] == ["t1", "prod-sp"]
    assert out[1]["except_principals"] == [] and out[1]["to_principals"] == ["dev-sp"]


# ── group_map renames the groups the policies name ──────────────────────────

PROD_TIERS = ["prod_ops", "dev_analysts"]
PROD_ENV_GROUP_MAP = PROD_ENV + 'group_map = { dev_ops = "prod_ops" }\n'


@pytest.mark.parametrize("ci", ["", "true"])
def test_promote_to_maps_groups_with_policies_tiers_and_acls_and_check_passes(repo, tmp_path, ci):
    _promote_to_prod(repo, tmp_path, env_text=PROD_ENV_GROUP_MAP)

    prod = _envs(repo) / "prod"
    env = hcl2.loads((prod / "env.auto.tfvars").read_text())
    assert env["access_tier_groups"] == PROD_TIERS
    assert env["genie_spaces"][0]["acl_groups"] == PROD_TIERS
    assert env["table_readers"] == {"ppay.s.t": PROD_TIERS}
    for rel in ("generated/abac.auto.tfvars", "data_access/abac.auto.tfvars", "abac.auto.tfvars"):
        assert list(hcl2.loads((prod / rel).read_text())["groups"]) == PROD_TIERS, rel
    account = hcl2.loads((_envs(repo) / "account" / "abac.auto.tfvars").read_text())
    assert "prod_ops" in account["groups"]
    for rel, policies in _policies(prod).items():
        named = {n for p in policies for f in ("to_principals", "except_principals") for n in p.get(f) or []}
        assert "prod_ops" in named and named <= set(PROD_TIERS) | {DEPLOYER, "account users"}, rel
    for path in prod.rglob("*"):
        if path.is_file():
            text = path.read_text()
            if path.name == "env.auto.tfvars":
                text = text.replace('"dev_ops" = "prod_ops"', "")
            assert '"dev_ops"' not in text, path
    _assert_check_and_promote_check_pass(repo, tmp_path, ci)


def test_promote_to_refuses_a_group_map_onto_a_group_dev_already_has(repo, tmp_path):
    prod = _envs(repo) / "prod"
    prod.mkdir()
    (prod / "env.auto.tfvars").write_text(PROD_ENV + 'group_map = { dev_ops = "dev_analysts" }\n')
    (prod / "auth.auto.tfvars").write_text(_auth(DEPLOYER, host=""))

    result = _make(repo, tmp_path, "promote-to", "ENV=prod")

    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "group_map would give groups 'dev_ops', 'dev_analysts' the one name 'dev_analysts'" in out, out
    assert not (prod / "generated" / "abac.auto.tfvars").exists()
    assert not (prod / "data_access" / "abac.auto.tfvars").exists()


def test_group_keys_rename_bare_and_quoted_keys_and_group_members():
    sys.path.insert(0, str(SHARED / "scripts"))
    from remap_generated_config import map_group_keys

    text = ('groups = {\n  dev_ops = {\n    description = "a = { b }"\n  }\n  "dev x" = { description = "" }\n'
            '  keep = {}\n}\ngroup_members = {\n  dev_ops = ["u1"]\n}\n')

    out = hcl2.loads(map_group_keys(text, {"dev_ops": "prod_ops", "dev x": "prod x"}, set()))

    assert list(out["groups"]) == ["prod_ops", "prod x", "keep"]
    assert out["groups"]["prod_ops"]["description"] == "a = { b }"
    assert out["group_members"] == {"prod_ops": ["u1"]}
