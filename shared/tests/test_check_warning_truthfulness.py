from pathlib import Path


ROOT = Path(__file__).parents[2]
WORKSPACE = (ROOT / "shared" / "roots" / "workspace" / "main.tf").read_text()
DATA_ACCESS = (ROOT / "shared" / "roots" / "data_access" / "main.tf").read_text()
CHECK_ENV = (ROOT / "shared" / "scripts" / "check_env.py").read_text()


def test_shared_deterministic_setting_is_declared_by_both_env_roots():
    declaration = 'variable "deployer_principal_id"'
    assert declaration in WORKSPACE
    assert declaration in DATA_ACCESS


def test_only_structural_check_opts_into_expected_withholding():
    assert 'variable "expected_pre_release_withholding"' in WORKSPACE
    assert "var.expected_pre_release_withholding ||" in WORKSPACE
    assert '"TF_VAR_expected_pre_release_withholding": "true"' in CHECK_ENV
    assert "empty-state pre-release plan is expected" in CHECK_ENV
    assert "plans and applies still fail when CAN_RUN is unexpectedly withheld" in CHECK_ENV
