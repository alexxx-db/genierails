from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from auth_configured import AuthStatus, auth_configured, auth_status  # noqa: E402

import pytest


def test_auth_configured_requires_non_placeholder_workspace_oauth_fields(tmp_path):
    auth = tmp_path / "auth.auto.tfvars"
    auth.write_text('databricks_client_id = ""\ndatabricks_workspace_host = "<host>"\n')
    assert not auth_configured(auth)
    auth.write_text('databricks_client_id = "client"\n'
                    'databricks_workspace_host = "https://dbc.example.com"\n'
                    'databricks_client_secret = "secret"\n')
    assert auth_configured(auth)


def test_auth_configured_requires_client_secret(tmp_path):
    auth = tmp_path / "auth.auto.tfvars"
    auth.write_text('databricks_client_id = "client"\n'
                    'databricks_workspace_host = "https://dbc.example.com"\n')
    assert not auth_configured(auth)


def test_missing_auth_is_incomplete(tmp_path):
    auth = tmp_path / "missing.auto.tfvars"
    assert auth_status(auth) is AuthStatus.INCOMPLETE
    assert not auth_configured(auth)


def test_malformed_auth_is_distinct_from_incomplete(tmp_path):
    auth = tmp_path / "auth.auto.tfvars"
    auth.write_text('databricks_client_secret = "unterminated\n')
    assert auth_status(auth) is AuthStatus.MALFORMED


@pytest.mark.parametrize("value", ["<client-id>", "your-client-id", "CHANGEME", "placeholder"])
def test_auth_configured_rejects_common_placeholders(tmp_path, value):
    auth = tmp_path / "auth.auto.tfvars"
    auth.write_text(f'databricks_client_id = "{value}"\n'
                    'databricks_workspace_host = "https://dbc.example.com"\n'
                    'databricks_client_secret = "secret"\n')
    assert not auth_configured(auth)
