import os
import subprocess
import sys
from pathlib import Path

import generate_abac


SHARED = Path(__file__).parents[1]


def _fake_sdk(root: Path) -> Path:
    sdk = root / "fake-sdk" / "databricks" / "sdk"
    sdk.mkdir(parents=True)
    (sdk.parent / "__init__.py").write_text("")
    (sdk / "useragent.py").write_text("")
    (sdk / "config.py").write_text(
        "class Config:\n"
        "    def __init__(self, **kwargs): self.__dict__.update(kwargs)\n"
    )
    (sdk / "__init__.py").write_text(
        "class WorkspaceClient:\n"
        "    def __init__(self, **kwargs):\n"
        "        raise RuntimeError('SDK MUST NOT BE CALLED')\n"
    )
    return sdk.parent.parent


def _run_generate(tmp_path: Path, auth_text: str) -> subprocess.CompletedProcess[str]:
    auth = tmp_path / "envs" / "dev" / "auth.auto.tfvars"
    auth.parent.mkdir(parents=True)
    auth.write_text(auth_text)
    config = tmp_path / "ambient.cfg"
    config.write_text(
        "[DEFAULT]\n"
        "host = https://wrong-profile.example\n"
        "token = wrong-profile-token\n"
    )
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "DATABRICKS_CONFIG_FILE": str(config),
        "DATABRICKS_HOST": "https://wrong-env.example",
        "DATABRICKS_TOKEN": "wrong-env-token",
        "PIP_NO_INDEX": "1",
        "PYTHONPATH": os.pathsep.join([str(_fake_sdk(tmp_path)), str(SHARED)]),
    })
    return subprocess.run(
        [sys.executable, str(SHARED / "generate_abac.py"), "--auth-file", str(auth)],
        text=True, capture_output=True, env=env, timeout=30,
    )


def test_empty_auth_refuses_before_sdk_even_with_ambient_auth(tmp_path):
    result = _run_generate(tmp_path, "# empty template\n")

    assert result.returncode == 1
    assert "fill in" in result.stderr
    assert "envs/dev/auth.auto.tfvars" in result.stderr
    assert "SDK MUST NOT BE CALLED" not in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr


def test_invalid_auth_never_prints_secret(tmp_path):
    secret = "never-echo-this-client-secret"
    result = _run_generate(
        tmp_path,
        'databricks_workspace_host = "https://env.example"\n'
        'databricks_client_id = "env-client"\n'
        f'databricks_client_secret = "{secret}\n',
    )

    assert result.returncode == 1
    assert "fill in" in result.stderr
    assert secret not in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr


def test_workspace_config_uses_env_auth_and_ignores_ambient(monkeypatch):
    import databricks.sdk
    import databricks.sdk.config

    captured = {}

    class FakeConfig:
        def __init__(self, **kwargs):
            captured["config"] = kwargs

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

    monkeypatch.setenv("DATABRICKS_HOST", "https://wrong-env.example")
    monkeypatch.setenv("DATABRICKS_TOKEN", "wrong-env-token")
    monkeypatch.setattr(databricks.sdk.config, "Config", FakeConfig)
    monkeypatch.setattr(databricks.sdk, "WorkspaceClient", FakeClient)
    auth = {
        "databricks_workspace_host": "https://selected-env.example",
        "databricks_client_id": "selected-client",
        "databricks_client_secret": "selected-secret",
    }

    try:
        generate_abac.fetch_tables_from_databricks([], auth)
    except SystemExit as exc:
        assert exc.code == 1

    assert captured["config"] == {
        "host": "https://selected-env.example",
        "client_id": "selected-client",
        "client_secret": "selected-secret",
        "auth_type": "oauth-m2m",
        "product": "genierails",
        "product_version": "0.1.0",
    }
    assert set(captured["client"]) == {"config"}


def test_make_generate_preflight_precedes_agent_sdk_hook():
    makefile = (SHARED / "Makefile.shared").read_text()
    target = makefile[makefile.index("generate:"):makefile.index("# Agents (", makefile.index("generate:"))]
    assert target.index("scripts/auth_configured.py") < target.index("import-new --check")
