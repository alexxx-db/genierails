"""terraform_layer.sh hands Terraform no Databricks or Azure auth from the shell.

The roots' providers take host and credentials from the env's auth.auto.tfvars,
but the Databricks provider also reads DATABRICKS_*, ARM_* and ~/.databrickscfg:
a stray token fails "more than one authorization method", and a DATABRICKS_HOST
fills a host the env leaves empty. Terraform here is a fake on PATH that records
the auth variables of every call (init included); provisioners inherit its env.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "shared" / "scripts" / "terraform_layer.sh"
AZURE = 'backend = "azurerm"\nresource_group_name = "r"\nstorage_account_name = "s"\ncontainer_name = "c"\n'
S3 = 'backend = "s3"\nbucket = "b"\nregion = "r"\ndynamodb_table = "locks"\n'
FAKE_TERRAFORM = """#!/usr/bin/env bash
{ printf '== %s\\n' "$*"; env | grep -E '^(DATABRICKS|ARM)_' | sort; } >> "$FAKE_TF_LOG"
exit 0
"""
AMBIENT = {
    "DATABRICKS_HOST": "https://ambient-wrong-ws.invalid",
    "DATABRICKS_TOKEN": "dapi-ambient",
    "DATABRICKS_CONFIG_PROFILE": "operator",
    "DATABRICKS_CLIENT_ID": "ambient-sp",
    "DATABRICKS_CLIENT_SECRET": "ambient-secret",
    "DATABRICKS_ACCOUNT_ID": "ambient-account",
    "DATABRICKS_AUTH_TYPE": "pat",
    "DATABRICKS_AZURE_RESOURCE_ID": "/subscriptions/x",
    "ARM_CLIENT_ID": "arm-client",
    "ARM_CLIENT_SECRET": "arm-secret",
    "ARM_TENANT_ID": "arm-tenant",
    "ARM_USE_MSI": "true",
}


def _calls(log: Path) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []
    for line in log.read_text().splitlines():
        if line.startswith("== "):
            calls.append((line[3:], {}))
        else:
            key, _, value = line.partition("=")
            calls[-1][1][key] = value
    return calls


def _run(tmp_path, layer, command, *args, env_dir=None, **extra):
    env_dir = env_dir or tmp_path / "envs" / "dev"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "auth.auto.tfvars").write_text('databricks_workspace_host = "https://env-file.invalid"\n')
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    # An operator profile the provider would otherwise read (DATABRICKS_CONFIG_FILE unset).
    (home / ".databrickscfg").write_text("[DEFAULT]\nhost = https://profile.invalid\ntoken = dapi-profile\n")
    bin_dir = tmp_path / "bin"
    if not bin_dir.is_dir():
        bin_dir.mkdir()
        (bin_dir / "terraform").write_text(FAKE_TERRAFORM)
        (bin_dir / "terraform").chmod(0o755)
        for name in ("python3", "python"):
            (bin_dir / name).symlink_to(sys.executable)
    log = tmp_path / "tf.log"
    log.write_text("")
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HOME": str(home), "LANG": "C",
           "FAKE_TF_LOG": str(log), "LAYER_ENV_DIR": str(env_dir), "ENVS_DIR": str(env_dir.parent),
           "DATABRICKS_USER_AGENT_EXTRA": "genierails/0.1.0", **AMBIENT, **extra}
    proc = subprocess.run([str(RUNNER), layer, "dev", command, *args], env=env, text=True,
                          capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = _calls(log)
    assert calls and calls[0][0].startswith("init "), calls
    return calls


@pytest.mark.parametrize("layer,command,args", [
    ("workspace", "plan", ()),
    ("workspace", "apply", ("-auto-approve",)),
    ("workspace", "import", ("databricks_group.g", "123")),
    ("workspace", "console", ()),
    ("workspace", "state-list", ()),
    ("workspace", "state-pull", ()),
    ("workspace", "show", ("-json", "plan.tfplan")),
    ("workspace", "apply-plan", ("plan.tfplan",)),
    ("data_access", "plan", ()),
    ("data_access", "apply", ("-auto-approve",)),
    ("account", "plan", ()),
    ("account", "apply", ("-auto-approve",)),
])
def test_no_ambient_databricks_or_arm_auth_reaches_terraform(tmp_path, layer, command, args):
    env_dir = tmp_path / "envs" / "dev" / ("data_access" if layer == "data_access" else "")
    if layer == "account":
        env_dir = tmp_path / "envs" / "account"
    calls = _run(tmp_path, layer, command, *args, env_dir=env_dir)
    for argv, auth_env in calls:
        assert auth_env == {"DATABRICKS_CONFIG_FILE": "/dev/null",
                            "DATABRICKS_USER_AGENT_EXTRA": "genierails/0.1.0"}, argv


@pytest.mark.parametrize("auth_type", ["github-oidc", "azure-devops-oidc"])
def test_pipeline_oidc_auth_type_still_reaches_terraform(tmp_path, auth_type):
    # The pipeline samples write an IDs-only auth file and pick OIDC this way.
    calls = _run(tmp_path, "workspace", "plan", DATABRICKS_AUTH_TYPE=auth_type)
    for argv, auth_env in calls:
        assert auth_env.get("DATABRICKS_AUTH_TYPE") == auth_type, argv
        assert "DATABRICKS_TOKEN" not in auth_env and "DATABRICKS_HOST" not in auth_env, argv


@pytest.mark.parametrize("auth_type", ["pat", "databricks-cli", "oauth-m2m", "azure-cli"])
def test_other_ambient_auth_types_are_dropped(tmp_path, auth_type):
    calls = _run(tmp_path, "workspace", "plan", DATABRICKS_AUTH_TYPE=auth_type)
    assert all("DATABRICKS_AUTH_TYPE" not in auth_env for _, auth_env in calls)


def test_azurerm_backend_keeps_the_arm_identity_it_reads_state_with(tmp_path):
    env_dir = tmp_path / "azure" / "envs" / "dev"
    env_dir.mkdir(parents=True)
    (env_dir / "remote_state.hcl").write_text(AZURE)
    calls = _run(tmp_path, "workspace", "state-list", env_dir=env_dir, ARM_USE_OIDC="true")
    assert "-backend-config=storage_account_name=s" in calls[0][0]
    for argv, auth_env in calls:
        assert {k: v for k, v in auth_env.items() if k.startswith("ARM_")} == {
            "ARM_CLIENT_ID": "arm-client", "ARM_CLIENT_SECRET": "arm-secret",
            "ARM_TENANT_ID": "arm-tenant", "ARM_USE_MSI": "true", "ARM_USE_OIDC": "true"}, argv
        assert not {"DATABRICKS_TOKEN", "DATABRICKS_HOST", "DATABRICKS_CONFIG_PROFILE"} & set(auth_env), argv
        assert auth_env["DATABRICKS_CONFIG_FILE"] == "/dev/null", argv


def test_s3_backend_drops_arm_variables(tmp_path):
    env_dir = tmp_path / "aws" / "envs" / "dev"
    env_dir.mkdir(parents=True)
    (env_dir / "remote_state.hcl").write_text(S3)
    calls = _run(tmp_path, "workspace", "state-list", env_dir=env_dir)
    assert "-backend-config=bucket=b" in calls[0][0]
    assert all(not any(k.startswith("ARM_") for k in auth_env) for _, auth_env in calls)
