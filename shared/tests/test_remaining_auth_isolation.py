import os
from pathlib import Path
from unittest.mock import ANY

import pytest

from scripts import audit_schema_drift, custom_masks, genie_roundtrip_live, reader_inventory
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
    raise AssertionError(name)


@pytest.mark.parametrize("script", [
    "audit_schema_drift", "custom_masks", "evidence_report", "reader_inventory",
    "genie_roundtrip_live",
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
    with pytest.raises(ClientCreated):
        _invoke(script, auth, monkeypatch)
    assert captured == {
        "host": "https://selected.example",
        "client_id": "selected-client",
        "client_secret": "selected-secret",
        "auth_type": "oauth-m2m",
        **({"product": "genierails", "product_version": "0.1.0"} if script == "audit_schema_drift" else {}),
        **({"product": "genierails-evidence", "product_version": "1.0"} if script == "evidence_report" else {}),
        "client_kwargs": {"config": ANY},
    }


@pytest.mark.parametrize("script", [
    "audit_schema_drift", "custom_masks", "evidence_report", "reader_inventory",
    "genie_roundtrip_live",
])
def test_empty_auth_refuses_before_any_client(script, tmp_path, monkeypatch):
    auth = tmp_path / "auth.auto.tfvars"
    auth.write_text("# empty\n")
    created = []
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda **kwargs: created.append(kwargs))
    with pytest.raises((RuntimeError, custom_masks.Refused), match="fill in"):
        _invoke(script, auth, monkeypatch)
    assert created == []
