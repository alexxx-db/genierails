"""make check and make promote-to CHECK=1 are read-only (real make, real git).

Each test builds a throwaway repo with the real aws/Makefile and shared/, runs
make the way CI would, and proves envs/ is byte-identical afterwards and git
sees no change. Terraform is a stub on PATH, so the structural plan runs
without credentials.
"""

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "shared"

if shutil.which("make") is None or shutil.which("git") is None:
    pytest.skip("make and git are required", allow_module_level=True)

DEV_ENV = '''governance_mode = "deterministic"
genie_spaces = [
  { genie_space_id = "s1", uc_tables = ["paycat.s.t"] },
]
'''
DEV_GENERATED = '''groups = { pay_group = {} }
tag_policies = [
  { key = "gr_treatment", description = "t", values = ["account_last4"] },
]
tag_assignments = [
  { entity_type = "columns", entity_name = "paycat.s.t.account_id", tag_key = "gr_treatment", tag_value = "account_last4" },
]
fgac_policies = [
  { name = "pay_mask", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "paycat", to_principals = ["pay_group"], comment = "c", match_condition = "hasTagValue('gr_treatment', 'account_last4')", match_alias = "a", function_name = "mask_account_number", function_catalog = "paycat", function_schema = "s" },
]
genie_space_configs = {
  Payments = { title = "Payments" }
}
genie_space_id_to_name = { s1 = "Payments" }
'''
MASKING_SQL = '''USE CATALOG paycat;
USE SCHEMA s;
CREATE OR REPLACE FUNCTION mask_account_number(v STRING) RETURNS STRING RETURN CONCAT('****', RIGHT(v, 4));
'''
GITIGNORE = "auth.auto.tfvars\n*.tfstate*\n.terraform/\n.*.json\n!.genie_space_id\nclassification.auto.tfvars\n"


def _b64(value):
    return '"' + base64.b64encode(json.dumps(value).encode()).decode() + '"'


# Answers the coverage check's and CAN_RUN check's terraform console queries
# like an empty state would, and logs every plan; no provider ever runs.
FAKE_TERRAFORM = f"""#!/bin/sh
case "$1" in
  console)
    case "$PWD" in
      */data_access) echo '{_b64({"fingerprint": "f", "grant_tables": [], "acknowledged_columns": [], "needs_gate": False, "new_grants": []})}' ;;
      *) echo '{_b64({"groups": {}, "blocker": "no state", "widening": {}, "missing": {}, "applied": {}})}' ;;
    esac ;;
  plan) echo "plan $(basename "$PWD")" >> "$FAKE_TF_LOG"; echo "FAKE PLAN" ;;
esac
exit 0
"""


def _env(tmp_path, **extra):
    drop = ("MAKEFLAGS", "MAKELEVEL", "MFLAGS", "ENV", "FROM", "CATALOG_MAP", "CHECK", "GENIERAILS_ALLOW_CI_APPLY",
            "CI", "TF_BUILD", "JENKINS_URL", "GITLAB_CI", "BUILDKITE", "CIRCLECI")
    env = {k: v for k, v in os.environ.items() if k not in drop and not k.startswith("DATABRICKS_")}
    env.update(PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}", FAKE_TF_LOG=str(tmp_path / "tf.log"),
               HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE="1", **extra)
    return env


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t",
                           *args],
                          text=True, capture_output=True, check=True).stdout


def _make(repo, tmp_path, *args, **env):
    return subprocess.run(["make", "--no-print-directory", "-C", str(repo / "aws"), *args],
                          text=True, capture_output=True, env=_env(tmp_path, **env), timeout=300)


def _snapshot(envs):
    return {str(p.relative_to(envs)): (os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file() else None)
            for p in sorted(envs.rglob("*"))}


@pytest.fixture
def repo(tmp_path):
    """A committed dev env: generate output plus the split promote writes."""
    repo = tmp_path / "repo"
    dev = repo / "aws" / "envs" / "dev"
    (dev / "generated").mkdir(parents=True)
    (repo / "aws" / "envs" / "account").mkdir()
    shutil.copy2(ROOT / "aws" / "Makefile", repo / "aws" / "Makefile")
    (repo / "shared").symlink_to(SHARED)
    (dev / "env.auto.tfvars").write_text(DEV_ENV)
    (dev / "generated" / "abac.auto.tfvars").write_text(DEV_GENERATED)
    (dev / "generated" / "masking_functions.sql").write_text(MASKING_SQL)
    (repo / ".gitignore").write_text(GITIGNORE)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "terraform").write_text(FAKE_TERRAFORM)
    (bin_dir / "terraform").chmod(0o755)
    assert _make(repo, tmp_path, "promote", "ENV=dev").returncode == 0
    subprocess.run([sys.executable, str(SHARED / "scripts" / "sticky_governance.py"), "record",
                    "--env-dir", str(dev), "--config", str(dev / "generated" / "abac.auto.tfvars")], check=True)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "dev")
    return repo


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def promoted(repo, tmp_path):
    prod = _envs(repo) / "prod"
    prod.mkdir()
    (prod / "env.auto.tfvars").write_text('governance_mode = "deterministic"\n')
    result = _make(repo, tmp_path, "promote-to", "ENV=prod", "FROM=dev", "CATALOG_MAP=paycat=ppay")
    assert result.returncode == 0, result.stdout + result.stderr
    _commit(repo, "promote")
    return repo


def _envs(repo):
    return repo / "aws" / "envs"


def _state(repo):
    """Every byte and symlink under envs/, plus git's view including ignored files."""
    status = _git(repo, "status", "--porcelain", "--ignored", "--untracked-files=all")
    return _snapshot(_envs(repo)), status


_baseline = _state


def _assert_read_only(repo, before):
    assert _state(repo) == before


@pytest.mark.parametrize("ci", ["", "true"])
def test_check_passes_on_a_clean_generated_tree_and_writes_nothing(repo, tmp_path, ci):
    before = _baseline(repo)

    result = _make(repo, tmp_path, "check", "ENV=dev", **({"CI": ci} if ci else {}))

    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "OK: generated files (envs/dev): committed files match." in out
    assert "STRUCTURAL plan: envs/dev as committed, against EMPTY state" in out
    assert "--- check 5/6: mask-library unit tests ---" in out and " passed in " in out
    assert "=== make check ENV=dev: passed ===" in out
    assert "applying targets cannot run in CI" not in out
    assert (tmp_path / "tf.log").read_text().split("\n")[:2] == ["plan data_access", "plan workspace"]
    _assert_read_only(repo, before)
    assert _git(repo, "status", "--porcelain") == ""


def test_check_fails_when_a_generated_file_is_hand_edited(repo, tmp_path):
    # Still valid config, so only the regeneration catches it.
    split = _envs(repo) / "dev" / "data_access" / "abac.auto.tfvars"
    text = split.read_text()
    assert 'comment = "c"' in text or '"c"' in text
    split.write_text(text.replace('"c"', '"hand edited"', 1))
    _commit(repo, "hand edit")
    before = _baseline(repo)

    result = _make(repo, tmp_path, "check", "ENV=dev", CI="true")

    assert result.returncode != 0
    assert "envs/dev/data_access/abac.auto.tfvars: differs from the regenerated file" in result.stderr
    assert "Generated files are never hand-edited. Run make generate ENV=dev" in result.stderr
    assert "=== make check ENV=dev: FAILED (generated files (envs/dev)) ===" in result.stderr
    assert not (tmp_path / "tf.log").exists()
    _assert_read_only(repo, before)


def test_check_fails_when_the_governed_table_record_is_not_committed(repo, tmp_path):
    _git(repo, "rm", "-q", "aws/envs/dev/generated/governed_tables.json")
    _commit(repo, "drop record")
    before = _baseline(repo)

    result = _make(repo, tmp_path, "check", "ENV=dev")

    assert result.returncode != 0
    assert "envs/dev/generated/governed_tables.json: missing from the commit" in result.stderr
    _assert_read_only(repo, before)


def test_check_notes_uncommitted_split_files_without_failing(repo, tmp_path):
    _git(repo, "rm", "-q", "aws/envs/dev/abac.auto.tfvars")
    _commit(repo, "split not committed")
    before = _baseline(repo)

    result = _make(repo, tmp_path, "check", "ENV=dev")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "NOTE: envs/dev/abac.auto.tfvars is not committed; the applying commands write it." in result.stdout
    _assert_read_only(repo, before)


def test_legacy_check_skips_regeneration_with_a_note(repo, tmp_path):
    env_file = _envs(repo) / "dev" / "env.auto.tfvars"
    env_file.write_text(env_file.read_text().replace('governance_mode = "deterministic"\n', ""))
    split = _envs(repo) / "dev" / "data_access" / "abac.auto.tfvars"
    split.write_text(split.read_text() + "\n# hand edit\n")
    _commit(repo, "legacy")
    before = _baseline(repo)

    result = _make(repo, tmp_path, "check", "ENV=dev", CI="true")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "NOTE: skipped: envs/dev is in legacy mode" in result.stdout
    _assert_read_only(repo, before)


def test_check_stops_on_the_first_failing_step(repo, tmp_path):
    split = _envs(repo) / "dev" / "data_access" / "abac.auto.tfvars"
    split.write_text(split.read_text() + "\nfgac_policies = [ {\n")
    _commit(repo, "broken")
    before = _baseline(repo)

    result = _make(repo, tmp_path, "check", "ENV=dev")

    assert result.returncode != 0
    assert "=== make check ENV=dev: FAILED (validate) ===" in result.stderr
    assert "--- check 2/6" not in result.stdout
    assert not (tmp_path / "tf.log").exists()
    _assert_read_only(repo, before)


@pytest.mark.parametrize("args, message", [
    (["ENV=account"], "This command is workspace-driven"),
    (["ENV=dev", "ENV_DIR=/tmp/elsewhere"], "is not envs/dev for ENV=dev; drop ENV_DIR"),
])
def test_check_refusals(repo, tmp_path, args, message):
    before = _baseline(repo)
    result = _make(repo, tmp_path, "check", *args)
    assert result.returncode != 0
    assert message in result.stderr
    _assert_read_only(repo, before)


@pytest.mark.parametrize("target", [["check", "ENV=dev"], ["promote-to", "ENV=prod", "CHECK=1"]])
def test_dry_run_runs_nothing(repo, tmp_path, target):
    before = _baseline(repo)
    result = _make(repo, tmp_path, "-n", *target)
    assert result.returncode == 0, result.stderr
    assert "check_env.py" in result.stdout
    assert "===" not in result.stdout
    _assert_read_only(repo, before)


@pytest.mark.parametrize("ci", ["", "true"])
def test_promote_check_passes_on_committed_promote_output_and_writes_nothing(promoted, tmp_path, ci):
    before = _baseline(promoted)

    result = _make(promoted, tmp_path, "promote-to", "ENV=prod", "CHECK=1", **({"CI": ci} if ci else {}))

    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "OK: promote-to ENV=prod CHECK=1: committed files match." in out
    assert "applying targets cannot run in CI" not in out
    _assert_read_only(promoted, before)
    assert _git(promoted, "status", "--porcelain") == ""


@pytest.mark.parametrize("path", ["prod/generated/abac.auto.tfvars", "prod/generated/expected_classification.json",
                                  "account/abac.auto.tfvars"])
def test_promote_check_fails_when_promote_output_is_hand_edited(promoted, tmp_path, path):
    edited = _envs(promoted) / path
    edited.write_text(edited.read_text().replace("account_last4", "redact"))
    before = _baseline(promoted)

    result = _make(promoted, tmp_path, "promote-to", "ENV=prod", "CHECK=1", CI="true")

    assert result.returncode != 0
    assert f"envs/{path}: differs from the regenerated file" in result.stderr
    assert "Run make promote-to ENV=prod and commit the result." in result.stderr
    _assert_read_only(promoted, before)


def test_promote_check_fails_when_the_promotion_is_not_committed(repo, tmp_path):
    prod = _envs(repo) / "prod"
    prod.mkdir()
    (prod / "env.auto.tfvars").write_text('governance_mode = "deterministic"\npromote_from = "dev"\n'
                                          'catalog_map = { "paycat" = "ppay" }\n')
    _commit(repo, "prod settings only")
    before = _baseline(repo)

    result = _make(repo, tmp_path, "promote-to", "ENV=prod", "CHECK=1")

    assert result.returncode != 0
    assert "envs/prod/generated/abac.auto.tfvars: missing from the commit" in result.stderr
    _assert_read_only(repo, before)


def test_promote_check_never_reads_prod_state_or_credentials(promoted, tmp_path):
    # Local state and credentials (ignored runtime files) never reach the copy;
    # a state-kept policy name would otherwise change promote-to's output.
    prod = _envs(promoted) / "prod"
    (prod / "data_access" / "terraform.tfstate").write_text("not json")
    (prod / "auth.auto.tfvars").write_text('databricks_workspace_host = "https://unreachable.invalid"\n'
                                          'databricks_client_id = "x"\ndatabricks_client_secret = "y"\n')
    before = _baseline(promoted)

    result = _make(promoted, tmp_path, "promote-to", "ENV=prod", "CHECK=1")

    assert result.returncode == 0, result.stdout + result.stderr
    _assert_read_only(promoted, before)


def test_check_prod_runs_promote_check(promoted, tmp_path):
    edited = _envs(promoted) / "prod" / "generated" / "abac.auto.tfvars"
    edited.write_text(edited.read_text().replace("account_last4", "redact"))
    _commit(promoted, "hand edit prod")
    before = _baseline(promoted)

    result = _make(promoted, tmp_path, "check", "ENV=prod", CI="true")

    assert result.returncode != 0
    assert "envs/prod/generated/abac.auto.tfvars: differs from the regenerated file" in result.stderr
    assert "FAILED (promote-to ENV=prod CHECK=1)" in result.stderr
    _assert_read_only(promoted, before)

