"""The roots' Databricks providers use only the env's auth, even beside ARM_*.

With an azurerm state backend, scripts/terraform_env.sh keeps ARM_* for the
backend, and the Databricks provider reads them too: ARM_CLIENT_SECRET next to
the env's SP secret failed "more than one authorization method configured:
azure and oauth". The roots pin auth_type (oauth-m2m for a client secret, the
kept pipeline OIDC type in a CI job, which terraform_layer.sh passes as
-var=databricks_pipeline_auth_type).

Each test builds a root from one real root's provider blocks and the
variables/locals they read, runs the real provider (plan, so it authenticates)
under the environment genierails_terraform_env hands Terraform, and reads which
auth method failed against the env's unresolvable hosts. Nothing leaves the
machine: the hosts are .invalid.
"""

import re
import subprocess
from pathlib import Path

import pytest

from tests.terraform_helpers import tf_env, tf_init

ROOT = Path(__file__).resolve().parents[2]
TERRAFORM_ENV = ROOT / "shared" / "scripts" / "terraform_env.sh"
LAYERS = ("data_access", "workspace", "account")
WS_HOST = "https://ws.invalid"
ACCOUNT_HOST = "https://accounts.invalid"
AUTH_VALUES = {
    "databricks_account_host": ACCOUNT_HOST,
    "databricks_workspace_host": WS_HOST,
    "databricks_account_id": "00000000-0000-0000-0000-000000000000",
    "databricks_workspace_id": "1",
    "databricks_client_id": "env-sp",
}
# What a deployment machine using the Azure backend has in its shell.
ARM = {"ARM_CLIENT_ID": "arm-client", "ARM_CLIENT_SECRET": "arm-secret",
       "ARM_TENANT_ID": "arm-tenant", "ARM_SUBSCRIPTION_ID": "arm-sub"}
# How each method fails against the .invalid hosts (databricks/databricks 1.111;
# workspace host, then account host).
METHODS = {
    "oauth-m2m": ("default auth: oidc: failed to get OAuth endpoints", "inner token: Post"),
    "github-oidc": ("default auth: failed to get OAuth endpoints", "default auth: missing ActionsIDTokenRequestURL"),
    "azure-devops-oidc": ("default auth: SYSTEM_ACCESSTOKEN env var not found",),
    "conflict": ("more than one authorization method",),
}


def _methods(output: str) -> list[str]:
    """The auth method of each provider's error, in order."""
    found = []
    for error in output.split("Error: ")[1:]:
        matches = [m for m, markers in METHODS.items() if any(k in error for k in markers)]
        assert len(matches) == 1, error
        if matches != ["conflict"]:
            assert WS_HOST in error or ACCOUNT_HOST in error, error
        found.append(matches[0])
    return found


def _blocks(text: str) -> list[str]:
    blocks, current = [], None
    for line in text.splitlines():
        if current is None and re.match(r"(provider|variable|locals)\b", line):
            current = []
        if current is not None:
            current.append(line)
            if line == "}":
                blocks.append("\n".join(current))
                current = None
    return blocks


def _provider_root(layer: str, dest: Path) -> tuple[list[str], set[str]]:
    blocks = _blocks((ROOT / "shared" / "roots" / layer / "main.tf").read_text())
    providers = [b for b in blocks if b.startswith('provider "databricks"')]
    assert len(providers) == 2, layer
    keep = list(providers)
    locals_ = {m for b in providers for m in re.findall(r"\blocal\.(\w+)", b)}
    keep += [b for b in blocks if b.startswith("locals")
             and any(re.search(rf"^\s*{name}\s*=", b, re.M) for name in locals_)]
    variables = {b.split('"')[1]: b for b in blocks if b.startswith("variable ")}
    wanted, pending = set(), set(re.findall(r"\bvar\.(\w+)", "\n".join(keep)))
    while pending:
        name = pending.pop()
        if name not in wanted:
            wanted.add(name)
            pending |= set(re.findall(r"\bvar\.(\w+)", variables[name]))
    keep += [variables[name] for name in sorted(wanted)]
    aliases = [re.search(r'alias\s*=\s*"(\w+)"', p).group(1) for p in providers]
    keep += [f'data "databricks_group" "{a}" {{\n  provider     = databricks.{a}\n  display_name = "x"\n}}'
             for a in aliases]
    dest.mkdir(parents=True)
    (dest / "main.tf").write_text(
        'terraform {\n  required_providers {\n    databricks = {\n'
        '      source  = "databricks/databricks"\n      version = "~> 1.111.0"\n    }\n  }\n}\n\n'
        + "\n\n".join(keep) + "\n")
    return aliases, wanted


@pytest.fixture(scope="module")
def roots(tmp_path_factory):
    """Each layer's provider root, initialized once."""
    base = tmp_path_factory.mktemp("provider-roots")
    built = {}
    for layer in LAYERS:
        root = base / layer
        aliases, variables = _provider_root(layer, root)
        env = tf_env(root)
        tf_init(root, env=env)
        built[layer] = (root, env, aliases, variables)
    return built


def _plan(roots, layer: str, *, secret: str = "env-sp-secret", backend: str = "azurerm",
          **shell: str) -> list[str]:
    root, env, aliases, variables = roots[layer]
    values = {**AUTH_VALUES, "databricks_client_secret": secret}
    (root / "auth.auto.tfvars").write_text(
        "".join(f'{k} = "{v}"\n' for k, v in values.items() if k in variables))
    # The runner's scrub (terraform_layer.sh passes "azurerm" when the env's
    # remote_state.hcl selects that backend) and the -var it then adds for a
    # kept pipeline OIDC, then the real provider.
    script = (f'. "{TERRAFORM_ENV}"; genierails_terraform_env {backend}; exec terraform plan -input=false -no-color'
              ' ${DATABRICKS_AUTH_TYPE:+"-var=databricks_pipeline_auth_type=$DATABRICKS_AUTH_TYPE"}')
    env = {k: v for k, v in env.items() if not k.startswith(("DATABRICKS_", "ARM_", "TF_VAR_"))}
    proc = subprocess.run(["bash", "-c", script], cwd=root, text=True, capture_output=True,
                          env={**env, "HOME": str(root), **ARM, **shell}, timeout=600)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    methods = _methods(output)
    assert len(methods) == len(aliases), output
    return methods


@pytest.mark.parametrize("layer", LAYERS)
def test_azure_backend_arm_secret_does_not_reach_the_env_sp_auth(roots, layer):
    assert _plan(roots, layer) == ["oauth-m2m", "oauth-m2m"]


@pytest.mark.parametrize("layer", LAYERS)
def test_s3_backend_pins_the_oauth_m2m_it_already_used(roots, layer):
    # Unpinned, the provider's default chain picked M2M too; a failed M2M on
    # the account host then read "cannot configure default credentials".
    assert _plan(roots, layer, backend="") == ["oauth-m2m", "oauth-m2m"]


@pytest.mark.parametrize("layer", LAYERS)
def test_github_oidc_wins_in_a_github_actions_job(roots, layer):
    # The pipeline samples' IDs-only auth file, plus the Azure backend's ARM_*.
    methods = _plan(roots, layer, secret="", DATABRICKS_AUTH_TYPE="github-oidc", GITHUB_ACTIONS="true")
    assert methods == ["github-oidc", "github-oidc"]


@pytest.mark.parametrize("secret", ["", "env-sp-secret"])
def test_azure_devops_oidc_wins_in_an_azure_pipelines_job(roots, secret):
    # Even over a secret in the auth file, as DATABRICKS_AUTH_TYPE did before the pin.
    methods = _plan(roots, "workspace", secret=secret, DATABRICKS_AUTH_TYPE="azure-devops-oidc", TF_BUILD="True")
    assert methods == ["azure-devops-oidc", "azure-devops-oidc"]


# (terraform_layer.sh refuses a caller's TF_VAR_ before the scrub; test.sh
# relies on the scrub dropping it.)
@pytest.mark.parametrize("shell", [
    {"DATABRICKS_AUTH_TYPE": "azure-devops-oidc"},          # no Azure Pipelines job
    {"TF_VAR_databricks_pipeline_auth_type": "github-oidc"},
    {"TF_VAR_databricks_pipeline_auth_type": "azure-devops-oidc", "TF_BUILD": "True"},
])
def test_a_local_shell_cannot_pick_the_pipeline_auth_type(roots, shell):
    assert _plan(roots, "workspace", **shell) == ["oauth-m2m", "oauth-m2m"]
