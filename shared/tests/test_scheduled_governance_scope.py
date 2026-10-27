"""Scheduled governance is out of scope for deterministic envs (T3).

- A deterministic env with enable_scheduled_governance = true is refused by
  every make entry point before anything is planned or written.
- A legacy env keeps the Terraform-managed job: make lets it through, and the
  data_access root plans no change against a job the base commit applied.
- The docs say so, give no make apply advice for deterministic envs, and never
  tell anyone to copy auth.auto.tfvars to the config source.
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.terraform_helpers import tf, tf_env, tf_init

ROOT = Path(__file__).parents[2]
SHARED = ROOT / "shared"
DOC = SHARED / "docs/scheduled-governance.md"
EXAMPLE = SHARED / "scheduled_governance.auto.tfvars.example"
REFERENCE = ROOT / "REFERENCE.md"
# origin/main before this change: the job as legacy envs deploy it today.
BASE_COMMIT = "3013986"

DETERMINISTIC = 'governance_mode = "deterministic"\n'
ENABLED = "enable_scheduled_governance = true\n"
LEGACY_JOB = ('governance_mode = "legacy"\n' + ENABLED
              + 'scheduled_governance_git_url = "https://github.com/example/repo"\n'
              + 'scheduled_governance_config_source = "/Volumes/main/governance/genierails/envs"\n')


def _refusal(env):
    return (f"ERROR: envs/{env}/env.auto.tfvars: enable_scheduled_governance = true is not supported in "
            "deterministic mode (GenieRails creates the scheduled-governance job only for legacy envs). "
            f"Remove it, and schedule make maintain ENV={env} (from the last released commit, clean tree, "
            f"on the deployment machine) or make audit-rulebook ENV={env} / make audit-schema ENV={env} "
            "with your own scheduler. See shared/docs/scheduled-governance.md.")


def _run_make(cloud_root, *args):
    env = {key: value for key, value in os.environ.items()
           if key not in ("MAKEFLAGS", "MAKELEVEL", "ENV", "SOURCE_ENV", "DEST_ENV")}
    return subprocess.run(
        ["make", "--no-print-directory", *args, f"CLOUD_ROOT={cloud_root}", f"SHARED_ROOT={SHARED}"],
        cwd=ROOT / "aws", text=True, capture_output=True, env=env, timeout=120,
    )


def _snapshot(root):
    entries = {}
    for path in sorted(root.rglob("*")):
        stat = path.lstat()
        detail = ("dir",) if path.is_dir() else ("file", hashlib.sha256(path.read_bytes()).hexdigest())
        entries[str(path.relative_to(root))] = (*detail, stat.st_mtime_ns)
    return entries


def _cloud(tmp_path, keyed_env, legacy=()):
    """dev and prod env files only, deterministic unless named in legacy; keyed_env enables the job."""
    cloud_root = tmp_path / "aws"
    for env in ("dev", "prod"):
        (cloud_root / "envs" / env).mkdir(parents=True)
        mode = 'governance_mode = "legacy"\n' if env in legacy else DETERMINISTIC
        text = mode + (ENABLED if env == keyed_env else "")
        (cloud_root / "envs" / env / "env.auto.tfvars").write_text(text)
    return cloud_root


ENTRY_POINTS = [
    ("prod", ("setup", "ENV=prod")),
    ("prod", ("validate", "ENV=prod")),
    ("prod", ("check", "ENV=prod")),
    ("prod", ("plan", "ENV=prod")),
    ("prod", ("apply", "ENV=prod")),
    ("prod", ("apply-governance", "ENV=prod")),
    ("prod", ("promote-to", "ENV=prod")),
    ("prod", ("promote-to", "ENV=prod", "CHECK=1")),
    ("dev", ("rehearse", "ENV=dev")),
    ("prod", ("release-plan", "ENV=prod")),
    ("prod", ("release", "ENV=prod")),
    ("prod", ("maintain", "ENV=prod")),
    ("prod", ("audit-rulebook", "ENV=prod")),
]


@pytest.mark.parametrize("env, target", ENTRY_POINTS, ids=[" ".join(t) for _, t in ENTRY_POINTS])
def test_deterministic_env_with_the_job_is_refused_before_anything_is_written(tmp_path, env, target):
    cloud_root = _cloud(tmp_path, env)
    before = _snapshot(cloud_root)
    result = _run_make(cloud_root, *target)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert _refusal(env) in result.stderr, output
    assert "===" not in result.stdout, output
    assert _snapshot(cloud_root) == before


PROMOTE = ("promote", "SOURCE_ENV=dev", "DEST_ENV=prod", "DEST_CATALOG_MAP=dev=prod")


def test_old_promote_from_a_deterministic_source_with_the_job_is_refused_before_anything_is_written(tmp_path):
    # A legacy destination passes make promote's destination guard, so the scheduled-governance
    # guard is the one that has to refuse the deterministic source.
    cloud_root = _cloud(tmp_path, "dev", legacy=("prod",))
    before = _snapshot(cloud_root)
    result = _run_make(cloud_root, *PROMOTE)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert _refusal("dev") in result.stderr, output
    assert "===" not in result.stdout, output
    assert _snapshot(cloud_root) == before


def test_old_promote_to_a_deterministic_destination_with_the_job_is_refused_before_anything_is_written(tmp_path):
    # make promote refuses every deterministic destination (it must use promote-to) before the
    # scheduled-governance guard runs; promote-to ENV=prod above reaches that guard.
    cloud_root = _cloud(tmp_path, "prod")
    before = _snapshot(cloud_root)
    result = _run_make(cloud_root, *PROMOTE)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert ("ERROR: make promote SOURCE_ENV=... DEST_ENV=prod is refused for deterministic destinations.\n"
            "       Use: make promote-to ENV=prod") in result.stderr, output
    assert "===" not in result.stdout, output
    assert _snapshot(cloud_root) == before


@pytest.mark.parametrize("target", ["setup", "validate", "check", "plan"])
def test_the_refusal_holds_in_ci(tmp_path, target):
    cloud_root = _cloud(tmp_path, "prod")
    before = _snapshot(cloud_root)
    result = _run_make(cloud_root, target, "ENV=prod", "CI=true")
    assert result.returncode != 0
    assert _refusal("prod") in result.stderr, result.stdout + result.stderr
    assert _snapshot(cloud_root) == before


def test_validate_env_config_refuses_it_directly(tmp_path):
    path = _cloud(tmp_path, "prod") / "envs/prod/env.auto.tfvars"
    path.write_text(path.read_text() + 'deployer_principal_id = "d0000000-0000-0000-0000-00000000d001"\n')
    result = subprocess.run([sys.executable, str(SHARED / "scripts/validate_env_config.py"), str(path)],
                            text=True, capture_output=True)
    assert result.returncode == 1
    assert _refusal("prod").removeprefix("ERROR: ") in result.stderr, result.stderr


@pytest.mark.parametrize("cfg, refused", [
    ({"governance_mode": "deterministic", "enable_scheduled_governance": True}, True),
    ({"governance_mode": "deterministic", "enable_scheduled_governance": "true"}, True),
    ({"governance_mode": "deterministic", "enable_scheduled_governance": False}, False),
    ({"governance_mode": "deterministic", "scheduled_governance_env": "prod"}, False),
    ({"governance_mode": "legacy", "enable_scheduled_governance": True}, False),
    ({"enable_scheduled_governance": True}, False),
])
def test_only_a_deterministic_env_that_enables_the_job_is_refused(cfg, refused):
    from scripts.scheduled_governance_scope import error
    assert (error(cfg, "prod") is not None) is refused


# -- legacy envs keep the job --------------------------------------------------

def test_legacy_env_with_the_job_gets_past_setup_and_validate_unchanged(tmp_path):
    cloud_root = tmp_path / "aws"
    env_file = cloud_root / "envs/prod/env.auto.tfvars"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(LEGACY_JOB)
    assert _run_make(cloud_root, "setup", "ENV=prod").returncode == 0
    result = _run_make(cloud_root, "validate", "ENV=prod")
    assert "=== Validate (prod) ===" in result.stdout, result.stdout + result.stderr
    assert "enable_scheduled_governance" not in result.stdout + result.stderr
    assert env_file.read_text() == LEGACY_JOB
    checked = subprocess.run([sys.executable, str(SHARED / "scripts/validate_env_config.py"), str(env_file)],
                             text=True, capture_output=True)
    assert checked.returncode == 0, checked.stderr


SHIM = """
terraform {
  required_providers {
    databricks = {
      source                = "databricks/databricks"
      configuration_aliases = [databricks.workspace]
    }
  }
}
variable "databricks_workspace_host" {
  type    = string
  default = "https://example.invalid"
}
"""

JOB_TEST = """
mock_provider "databricks" {
  alias = "workspace"
}

variables {
  enable_scheduled_governance              = true
  scheduled_governance_git_url             = "https://github.com/example/repo"
  scheduled_governance_config_source       = "/Volumes/main/governance/genierails/envs"
  scheduled_governance_notification_emails = ["governance@example.com"]
}

run "base_commit_applies_the_job" {
  state_key = "job"
  module {
    source = "./base"
  }
  providers = {
    databricks.workspace = databricks.workspace
  }
}

run "current_root_plans_no_change" {
  state_key = "job"
  command   = plan
  module {
    source = "./current"
  }
  providers = {
    databricks.workspace = databricks.workspace
  }
  assert {
    condition     = length(databricks_job.scheduled_governance) == 1
    error_message = "the legacy job must stay"
  }
}
"""

CHANGE = re.compile(r"# (\S+) (will be (?:created|destroyed|updated in-place)|must be replaced"
                    r"|will no longer be managed by Terraform)")


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform not installed")
def test_legacy_job_plans_unchanged_against_the_base_commit(tmp_path):
    shown = subprocess.run(["git", "show", f"{BASE_COMMIT}:shared/roots/data_access/scheduled_governance.tf"],
                           cwd=ROOT, text=True, capture_output=True)
    if shown.returncode != 0:
        message = f"base commit {BASE_COMMIT} is not in this clone (shallow checkout?)"
        if os.environ.get("REQUIRE_TERRAFORM_TESTS") == "1":
            pytest.fail(message)
        pytest.skip(message)
    harness = tmp_path / "harness"
    for name, text in (("base", shown.stdout),
                       ("current", (SHARED / "roots/data_access/scheduled_governance.tf").read_text())):
        (harness / name).mkdir(parents=True)
        (harness / name / "scheduled_governance.tf").write_text(text)
        (harness / name / "shim.tf").write_text(SHIM)
    (harness / "main.tf").write_text(SHIM)
    (harness / "tests").mkdir()
    (harness / "tests/job.tftest.hcl").write_text(JOB_TEST)
    env = tf_env(tmp_path)
    tf_init(harness, env=env)
    result = tf(harness, "test", "-no-color", "-verbose", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    planned = result.stdout.split('run "current_root_plans_no_change"', 1)[1]
    assert CHANGE.findall(planned) == [], planned
    assert "No changes." in planned, planned


# -- docs ----------------------------------------------------------------------

def _flat(path):
    return " ".join(path.read_text().split())


def _deterministic_section():
    text = DOC.read_text()
    start = text.index("## Deterministic envs")
    return text[start:text.index("\n## ", start + 1)]


def test_doc_tells_deterministic_envs_to_schedule_maintain_themselves():
    section = _deterministic_section()
    flat = " ".join(section.split())
    assert "GenieRails doesn't create a scheduled job for a deterministic env." in flat
    assert "from the last released commit with a clean tree" in flat
    assert "0 6 * * * cd /srv/genierails/aws && make maintain ENV=prod" in section
    assert "`make audit-schema ENV=<env>` and `make audit-rulebook ENV=<env>`" in flat
    assert "make apply" not in section
    assert "config_source" not in section and "Volume" not in section
    makefile = (SHARED / "Makefile.shared").read_text()
    for target in set(re.findall(r"make ([a-z][a-z-]*)", section)):
        assert re.search(rf"^{re.escape(target)}:", makefile, re.MULTILINE), target


def test_doc_marks_the_job_legacy_only_and_deprecated():
    assert "## Legacy envs: the Terraform-managed job (deprecated)" in DOC.read_text()
    assert "Scheduled governance job — LEGACY envs only (deprecated)" in EXAMPLE.read_text()
    assert "Deterministic envs: not supported." in EXAMPLE.read_text()
    reference = _flat(REFERENCE)
    assert "### `env.auto.tfvars`: scheduled governance (data_access root; legacy envs only, deprecated)" in reference
    assert ("| `enable_scheduled_governance` | bool | `false` | Legacy envs only (deprecated): create a scheduled "
            "job that runs the read-only governance checks. Refused in a deterministic env. |") in reference


@pytest.mark.parametrize("path, keep_out", [
    (DOC, "Leave `auth.auto.tfvars` and every other secret out of it"),
    (EXAMPLE, "Never put auth.auto.tfvars or any other # secret here"),
    (REFERENCE, "Never put `auth.auto.tfvars` or another secret there"),
], ids=["doc", "example", "reference"])
def test_no_doc_puts_the_auth_file_on_the_config_source(path, keep_out):
    flat = _flat(path)
    assert keep_out in flat
    assert "with auth.auto.tfvars, env.auto.tfvars" not in flat
    for sentence in re.split(r"(?<=[.;:])\s", flat):
        if "auth.auto.tfvars" in sentence and re.search(r"Volume|config source|config_source", sentence):
            assert re.search(r"[Nn]ever|Leave", sentence), sentence


@pytest.mark.parametrize("path", [DOC, EXAMPLE, SHARED / "roots/data_access/scheduled_governance.tf",
                                  SHARED / "scripts/run_scheduled_governance.py"],
                         ids=["doc", "example", "data_access-tf", "runner"])
def test_no_stale_envs_gitignored_claim(path):
    text = " ".join(path.read_text().split())
    assert not re.search(r"envs/`? is gitignored", text)
    assert ".gitignore'd" not in text
