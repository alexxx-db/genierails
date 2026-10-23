import os
from pathlib import Path
from unittest.mock import ANY

import pytest

from scripts import (audit_schema_drift, classification_completeness, custom_masks,
                     genie_adopt_preflight, genie_roundtrip_live, prepare_classification_config,
                     reader_inventory, remap_env_config, remap_generated_config,
                     sync_tag_policies, wait_for_tag_policies)
import deploy_masking_functions
import evidence_report


class ClientCreated(Exception):
    pass


def _invoke(name: str, auth: Path, monkeypatch):
    if name == "audit_schema_drift":
        return audit_schema_drift._get_sdk_client(auth.parent)
    if name == "custom_masks":
        return custom_masks.workspace_client(auth)
    if name == "evidence_report":
        return evidence_report.collect_live_state([], ["c.s.t"], "warehouse", auth)
    if name == "reader_inventory":
        return reader_inventory.DatabricksSource("warehouse", auth_file=auth)
    if name == "genie_roundtrip_live":
        monkeypatch.setenv("DATABRICKS_LIVE_TESTS", "1")
        return genie_roundtrip_live.main(["--space-id", "space", "--auth-file", str(auth)])
    if name == "classification_completeness":
        (auth.parent / "env.auto.tfvars").write_text('sql_warehouse_id = "warehouse"\n')
        return classification_completeness._live_classified_columns_query(auth.parent, 10**9)
    if name == "genie_adopt_preflight":
        return genie_adopt_preflight.get_status({"_auth_file": auth}, "space")
    if name == "deploy_masking_functions":
        deploy_masking_functions.load_credentials(str(auth), "https://selected.example")
        return deploy_masking_functions.deploy(str(auth.parent / "masking.sql"), "warehouse")
    if name == "sync_tag_policies":
        sync_tag_policies.WORK_DIR = auth.parent
        tfvars = auth.parent / "abac.auto.tfvars"
        tfvars.write_text('tag_policies = [{ key = "k", values = ["v"] }]\n')
        return sync_tag_policies.main([str(tfvars)])
    if name == "wait_for_tag_policies":
        wait_for_tag_policies.WORK_DIR = auth.parent
        tfvars = auth.parent / "abac.auto.tfvars"
        tfvars.write_text('tag_policies = [{ key = "k", values = ["v"] }]\n')
        monkeypatch.setattr("sys.argv", ["wait_for_tag_policies.py", str(tfvars)])
        return wait_for_tag_policies.main()
    if name == "remap_generated_config":
        return remap_generated_config.live_policy_lister(auth, timeout=1)("catalog")
    if name == "remap_env_config":
        return remap_env_config._discover_from_genie_api("space", auth)
    if name == "prepare_classification_config":
        (auth.parent / "env.auto.tfvars").write_text(
            'enable_classification = true\nuc_tables = ["catalog.schema.table"]\n')
        monkeypatch.setattr("sys.argv", ["prepare_classification_config.py", str(auth.parent)])
        return prepare_classification_config.main()
    raise AssertionError(name)


@pytest.mark.parametrize("script", [
    "audit_schema_drift", "custom_masks", "evidence_report", "reader_inventory",
    "genie_roundtrip_live",
    "classification_completeness", "genie_adopt_preflight", "deploy_masking_functions",
    "sync_tag_policies", "wait_for_tag_policies", "remap_generated_config",
    "remap_env_config", "prepare_classification_config",
])
def test_client_uses_only_selected_env_auth(script, tmp_path, monkeypatch):
    auth = tmp_path / "auth.auto.tfvars"
    auth.write_text(
        'databricks_workspace_host = "https://selected.example"\n'
        'databricks_client_id = "selected-client"\n'
        'databricks_client_secret = "selected-secret"\n'
    )
    config_file = tmp_path / "ambient.cfg"
    config_file.write_text("[wrong]\nhost=https://wrong-profile.example\ntoken=wrong-profile-token\n")
    monkeypatch.setenv("DATABRICKS_HOST", "https://wrong-env.example")
    monkeypatch.setenv("DATABRICKS_TOKEN", "wrong-env-token")
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "wrong")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config_file))
    captured = {}

    class Config:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    def client(**kwargs):
        captured["client_kwargs"] = kwargs
        raise ClientCreated

    monkeypatch.setattr("databricks.sdk.config.Config", Config)
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", client)
    try:
        _invoke(script, auth, monkeypatch)
    except ClientCreated:
        pass
    assert {key: captured[key] for key in (
        "host", "client_id", "client_secret", "auth_type", "client_kwargs"
    )} == {
        "host": "https://selected.example", "client_id": "selected-client",
        "client_secret": "selected-secret", "auth_type": "oauth-m2m",
        "client_kwargs": {"config": ANY},
    }
    assert "https://wrong-env.example" not in repr(captured)
    assert "wrong-env-token" not in repr(captured)


@pytest.mark.parametrize("script", [
    "audit_schema_drift", "custom_masks", "evidence_report", "reader_inventory",
    "genie_roundtrip_live",
    "classification_completeness", "genie_adopt_preflight", "deploy_masking_functions",
    "sync_tag_policies", "wait_for_tag_policies", "remap_generated_config",
    "remap_env_config", "prepare_classification_config",
])
def test_empty_auth_refuses_before_any_client(script, tmp_path, monkeypatch):
    auth = tmp_path / "auth.auto.tfvars"
    auth.write_text("# empty\n")
    created = []
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda **kwargs: created.append(kwargs))
    try:
        _invoke(script, auth, monkeypatch)
    except (RuntimeError, custom_masks.Refused, SystemExit, ValueError):
        pass
    assert created == []


@pytest.mark.parametrize("script", [
    "classification_completeness", "genie_adopt_preflight", "deploy_masking_functions",
    "sync_tag_policies", "wait_for_tag_policies", "remap_generated_config",
    "remap_env_config", "prepare_classification_config",
])
@pytest.mark.parametrize("variant", ["malformed", "non-string", "unreadable"])
def test_other_unusable_auth_never_creates_client(script, variant, tmp_path, monkeypatch):
    auth = tmp_path / "auth.auto.tfvars"
    if variant == "malformed":
        auth.write_text('databricks_client_secret = "unterminated\n')
    elif variant == "non-string":
        auth.write_text(
            'databricks_workspace_host = "https://selected.example"\n'
            'databricks_client_id = ["a"]\n'
            'databricks_client_secret = "selected-secret"\n')
    else:
        auth.mkdir()
    created = []
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda **kwargs: created.append(kwargs))
    try:
        _invoke(script, auth, monkeypatch)
    except (BaseException,):
        pass
    assert created == []
