"""make validate fails when any layer of the split config is broken.

It used to report only the workspace layer's result, so a data_access layer
that failed to parse still passed.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

ENV = 'genie_spaces = [\n  { genie_space_id = "s1", uc_tables = ["paycat.s.t"] },\n]\n'
GENERATED = '''groups = { pay_group = {} }
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
SQL = ("USE CATALOG paycat;\nUSE SCHEMA s;\nCREATE OR REPLACE FUNCTION mask_account_number(v STRING) "
       "RETURNS STRING RETURN CONCAT('****', RIGHT(v, 4));\n")


def _make(cloud, *args):
    env = {k: v for k, v in os.environ.items() if k not in ("MAKEFLAGS", "MAKELEVEL", "ENV")}
    return subprocess.run(["make", "--no-print-directory", "-C", str(cloud), *args, "ENV=dev"],
                          text=True, capture_output=True, env=env, timeout=120)


@pytest.fixture
def cloud(tmp_path):
    if shutil.which("make") is None:
        pytest.skip("make not installed")
    cloud = tmp_path / "aws"
    dev = cloud / "envs" / "dev"
    (dev / "generated").mkdir(parents=True)
    shutil.copy2(ROOT / "aws" / "Makefile", cloud / "Makefile")
    (tmp_path / "shared").symlink_to(ROOT / "shared")
    (dev / "env.auto.tfvars").write_text(ENV)
    (dev / "generated" / "abac.auto.tfvars").write_text(GENERATED)
    (dev / "generated" / "masking_functions.sql").write_text(SQL)
    result = _make(cloud, "promote")
    assert result.returncode == 0, result.stdout + result.stderr
    return cloud


def test_validate_passes_on_valid_layers(cloud):
    result = _make(cloud, "validate")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("layer", ["data_access/abac.auto.tfvars", "abac.auto.tfvars"])
def test_validate_fails_when_any_layer_is_broken(cloud, layer):
    broken = cloud / "envs" / "dev" / layer
    broken.write_text(broken.read_text() + "\nfgac_policies = [ {\n")

    result = _make(cloud, "validate")

    assert result.returncode != 0
    assert "Failed to parse" in result.stdout
