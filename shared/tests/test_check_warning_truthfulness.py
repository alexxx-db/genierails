import os
import subprocess
from pathlib import Path

from scripts.state_layout import STRUCTURAL_CHECK_TOKEN_ENV, mint_structural_check_token


ROOT = Path(__file__).parents[2]
WORKSPACE = (ROOT / "shared" / "roots" / "workspace" / "main.tf").read_text()
DATA_ACCESS = (ROOT / "shared" / "roots" / "data_access" / "main.tf").read_text()
CHECK_ENV = (ROOT / "shared" / "scripts" / "check_env.py").read_text()
RUNNER = ROOT / "shared" / "scripts" / "terraform_layer.sh"


def test_shared_deterministic_setting_is_declared_by_both_env_roots():
    declaration = 'variable "deployer_principal_id"'
    assert declaration in WORKSPACE
    assert declaration in DATA_ACCESS


def test_only_structural_check_opts_into_expected_withholding():
    assert 'variable "expected_pre_release_withholding"' in WORKSPACE
    assert "var.expected_pre_release_withholding ||" in WORKSPACE
    assert "mint_structural_check_token" in CHECK_ENV
    assert "empty-state pre-release plan is expected" in CHECK_ENV
    assert "plans and applies still fail when CAN_RUN is unexpectedly withheld" in CHECK_ENV


def test_runner_owns_structural_withholding_exemption(tmp_path):
    env_dir = tmp_path / "envs" / "dev"
    env_dir.mkdir(parents=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "terraform.log"
    terraform = fake_bin / "terraform"
    terraform.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\n')
    terraform.chmod(0o755)
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ENVS_DIR": str(env_dir.parent),
        "LAYER_ENV_DIR": str(env_dir),
    }

    ordinary = subprocess.run(
        [RUNNER, "workspace", "dev", "plan"], env=env, text=True, capture_output=True,
    )
    assert ordinary.returncode == 0, ordinary.stdout + ordinary.stderr
    assert "-var=expected_pre_release_withholding=false" in log.read_text()
    assert "expected_pre_release_withholding=true" not in log.read_text()

    token = mint_structural_check_token(env_dir)
    structural = subprocess.run(
        [RUNNER, "workspace", "dev", "plan"],
        env=env | {STRUCTURAL_CHECK_TOKEN_ENV: str(token)}, text=True, capture_output=True,
    )
    assert structural.returncode == 0, structural.stdout + structural.stderr
    assert "-var=expected_pre_release_withholding=true" in log.read_text()
    assert not token.exists()

    forged = subprocess.run(
        [RUNNER, "workspace", "dev", "plan"],
        env=env | {"TF_VAR_expected_pre_release_withholding": "true"},
        text=True, capture_output=True,
    )
    assert forged.returncode == 2
    assert "TF_VAR_expected_pre_release_withholding sets expected_pre_release_withholding" in forged.stderr
