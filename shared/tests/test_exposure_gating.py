import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from tests.terraform_helpers import shared_copy, tf, tf_env, tf_init


SHARED = Path(__file__).parents[1]


ROOTS = (SHARED / "roots/data_access/main.tf", SHARED / "roots/workspace/main.tf")


def _write_layer_auth(path, host):
    path.mkdir(exist_ok=True)
    (path / "auth.auto.tfvars").write_text(
        f'databricks_workspace_host = "{host}"\n'
        'databricks_client_id = "client"\n'
        'databricks_client_secret = "secret"\n'
    )


def test_retired_flag_is_declared_deprecated_in_the_roots_only_and_never_read():
    # Modules no longer know the flag; nothing can gate (or revoke) on it.
    for module in ("data_access", "workspace"):
        for path in (SHARED / "modules" / module).glob("*.tf"):
            source = path.read_text()
            assert "var.business_access_enabled" not in source, path
            assert 'variable "business_access_enabled"' not in source, path
    # The roots keep it declared for one release, so env.auto.tfvars files
    # and -var flags that still set it (true or false) don't fail.
    for path in ROOTS:
        source = path.read_text()
        start = source.index('variable "business_access_enabled"')
        body = source[start : source.index("}\n", start) + 2]
        assert "default     = null" in body
        assert "DEPRECATED and ignored" in body
        assert "false does NOT revoke access" in body
        assert "var.business_access_enabled" not in source


def test_workspace_business_acls_follow_the_gate_not_a_flag():
    source = (SHARED / "modules/workspace/main.tf").read_text()

    # Same for_each keys as before the retirement, minus the flag (and minus
    # a space whose every group is withheld).
    assert source.count("if contains(local.genie_space_acl_keys, k)") == 2
    # Two grant-bearing resources plus two removal-only resources for the
    # fully-withheld adopted-agent paths must all permit an empty exact sync.
    assert len(re.findall(r'GENIE_ALLOW_EMPTY_ACL\s*= "1"', source)) == 4
    # Opening or widening CAN_RUN still needs the coverage gate: without it
    # the new groups are withheld from both ACL resources.
    withheld = source[source.index("  genie_space_can_run_withheld = {"):source.index("  genie_space_acl_groups = {")]
    assert 'var.genie_exposure_blocker == ""' in withheld
    assert source.count("local.genie_space_acl_groups[each.key]") == 4  # triggers + set-acls, twice
    # Removing a group or space takes its CAN_RUN back.
    assert source.count('try(self.triggers.destroy_script, "bash ../../scripts/genie_space.sh")') == 2
    variables = (SHARED / "modules/workspace/variables.tf").read_text()
    assert re.search(r'variable "genie_destroy_script" \{.*?default\s*= "bash ../../scripts/genie_space.sh"', variables, re.S)


def _guard_workspace_config(tmp_path, env_file, *make_args, environ=None, nested=False):
    """Run the real guard; nested=True calls it the way apply/plan do (a
    recursive make), with make_args on the OUTER command line."""
    env_dir = tmp_path / "envs" / "dev"
    env_dir.mkdir(parents=True)
    (env_dir / "env.auto.tfvars").write_text(env_file)
    env = {k: v for k, v in os.environ.items()
           if k not in ("MAKEFLAGS", "MAKELEVEL", "APPLY_FLAGS", "TF_VAR_business_access_enabled")}
    env.update(environ or {})
    target = ["_guard-workspace-config"]
    if nested:
        wrapper = tmp_path / "nested.mk"
        wrapper.write_text('_nested-guard:\n\t@$(MAKE) --no-print-directory ENV="$(ENV)" _guard-workspace-config\n')
        target = ["-f", "Makefile", "-f", str(wrapper), "_nested-guard"]
    return subprocess.run(
        ["make", "--no-print-directory", *target, "ENV=dev",
         f"ENV_DIR={env_dir}", f"CLOUD_ROOT={tmp_path}", f"SHARED_ROOT={SHARED}", *make_args],
        cwd=SHARED.parent / "aws", text=True, capture_output=True, env=env,
    )


def _retired_flag_warnings(result):
    assert result.returncode == 0, result.stdout + result.stderr
    return [line for line in (result.stdout + result.stderr).splitlines() if "business_access_enabled" in line]


def _assert_one_warning(result, *sources):
    warnings = _retired_flag_warnings(result)
    assert len(warnings) == 1, result.stdout + result.stderr
    assert warnings[0].startswith("WARNING: business_access_enabled (")
    assert ") is deprecated and ignored" in warnings[0]
    assert "setting it false does not revoke access" in warnings[0]
    assert "remove the groups or acl_groups entries" in warnings[0]
    for source in sources:
        assert source in warnings[0], warnings[0]


@pytest.mark.parametrize("value", ["true", "false"])
def test_setting_the_retired_flag_warns_once_and_does_not_fail(tmp_path, value):
    result = _guard_workspace_config(tmp_path, f'business_access_enabled = {value}\nsql_warehouse_id = ""\n')
    _assert_one_warning(result, "envs/dev/env.auto.tfvars")
    # Warning only: the file is left exactly as it was.
    assert (tmp_path / "envs/dev/env.auto.tfvars").read_text().startswith(f"business_access_enabled = {value}")


@pytest.mark.parametrize("flags", [
    "-var=business_access_enabled=false",
    "-var=business_access_enabled=true",
    "-var business_access_enabled=false",
    "-parallelism=1 -var business_access_enabled=true -var=coverage_gate_max_age=6h",
])
@pytest.mark.parametrize("nested", [False, True])
def test_retired_flag_in_apply_flags_warns_once_without_a_file_setting(tmp_path, flags, nested):
    result = _guard_workspace_config(tmp_path, 'sql_warehouse_id = ""\n', f"APPLY_FLAGS={flags}", nested=nested)
    _assert_one_warning(result, "APPLY_FLAGS")
    assert "env.auto.tfvars" not in _retired_flag_warnings(result)[0]


@pytest.mark.parametrize("nested", [False, True])
def test_file_and_apply_flags_together_still_warn_exactly_once(tmp_path, nested):
    result = _guard_workspace_config(
        tmp_path, "business_access_enabled = true\n", "APPLY_FLAGS=-var=business_access_enabled=false",
        environ={"TF_VAR_business_access_enabled": "false"}, nested=nested)
    _assert_one_warning(result, "envs/dev/env.auto.tfvars", "APPLY_FLAGS", "TF_VAR_business_access_enabled")


def test_tf_var_environment_setting_warns_once(tmp_path):
    result = _guard_workspace_config(tmp_path, 'sql_warehouse_id = ""\n',
                                     environ={"TF_VAR_business_access_enabled": "true"})
    _assert_one_warning(result, "TF_VAR_business_access_enabled")


@pytest.mark.parametrize("flags", ["", "-var=coverage_gate_max_age=6h", "-var=other_business_access_enabled=true"])
def test_unset_retired_flag_prints_no_warning(tmp_path, flags):
    result = _guard_workspace_config(tmp_path, 'sql_warehouse_id = ""\n', f"APPLY_FLAGS={flags}")
    assert _retired_flag_warnings(result) == []


def test_genie_creation_replaces_only_on_host_and_keeps_no_credentials():
    source = (SHARED / "modules/workspace/main.tf").read_text()
    start = source.index('resource "terraform_data" "genie_space"')
    end = source.index('resource "null_resource" "genie_space_config"', start)
    block = source[start:end]
    resource = block[: block.index("\nremoved {")]
    triggers = re.search(r"triggers_replace = \{(.*?)\n  \}", resource, re.S).group(1)
    assert set(re.findall(r"^\s*(\w+)\s*=", triggers, re.M)) == {"host"}
    assert "ignore_changes" not in resource
    assert 'command = "${var.genie_script_path} create"' in resource
    # Create-time credentials come from variables and never enter state.
    assert "DATABRICKS_CLIENT_SECRET = var.databricks_client_secret" in resource
    assert "self.triggers_replace.client" not in resource and "self.input.client" not in resource
    assert 'command = "bash ../../scripts/genie_space.sh trash"' in resource
    assert "GENIE_ID_FILE       = self.input.id_file" in resource
    assert re.search(r"GENIE_ID_BASENAME\s+= basename\(self\.input\.id_file\)", resource)
    assert "GENIE_EXPECTED_HOST = self.triggers_replace.host" in resource
    # The pre-migration resource is forgotten, never destroyed (that would trash the agent).
    removed = block[block.index("\nremoved {"):]
    assert "from = null_resource.genie_space_create" in removed
    assert re.search(r"lifecycle \{\s*destroy = false\s*\}", removed)


def test_created_space_acl_reapplies_after_space_recreation():
    source = (SHARED / "modules/workspace/main.tf").read_text()
    start = source.index('resource "null_resource" "genie_space_acls_created"')
    block = source[start:]
    assert "space_create_id = terraform_data.genie_space[each.key].id" in block


def test_created_space_semantic_config_reapplies_after_space_recreation():
    source = (SHARED / "modules/workspace/main.tf").read_text()
    start = source.index('resource "null_resource" "genie_space_config"')
    end = source.index('resource "null_resource" "genie_space_acls_created"', start)
    block = source[start:end]
    assert "space_create_id = terraform_data.genie_space[each.key].id" in block


def test_trash_fails_loud_when_current_id_file_is_missing(tmp_path):
    env_dir = tmp_path / "env"
    _write_layer_auth(env_dir, "https://example.invalid")
    result = subprocess.run(
        ["bash", str(SHARED / "scripts/genie_space.sh"), "trash"],
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "LAYER_ENV_DIR": str(env_dir),
            "GENIE_ID_BASENAME": ".genie_space_id_payments",
            "DATABRICKS_HOST": "https://example.invalid",
        },
    )
    assert result.returncode != 0
    assert "Cannot identify Genie agent to trash" in result.stderr
    assert "orphan" in result.stderr


def test_trash_targets_original_host_not_current_host_on_mismatch(tmp_path):
    layer = tmp_path / "env"
    _write_layer_auth(layer, "https://w2.example")
    id_file = tmp_path / "space.id"
    id_file.write_text("space-123\n")
    calls = tmp_path / "curl.calls"
    curl = tmp_path / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$CURL_CALLS"\n'
        'url="${!#}"\n'
        'if [[ "$url" == "https://w1.example/oidc/v1/token" ]]; then\n'
        "  printf '{\"access_token\":\"token\"}\\n200\\n'\n"
        'elif [[ "$url" == "https://w1.example/api/2.0/genie/spaces/space-123" ]]; then\n'
        "  printf '{}\\n404\\n'\n"
        "else\n"
        "  printf '{}\\n404\\n'\n"
        "fi\n"
    )
    curl.chmod(0o755)
    result = subprocess.run(
        ["bash", str(SHARED / "scripts/genie_space.sh"), "trash"],
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "CURL_CALLS": str(calls),
            "LAYER_ENV_DIR": str(layer),
            "GENIE_ID_FILE": str(id_file),
            "GENIE_EXPECTED_HOST": "https://w1.example/",
            "DATABRICKS_HOST": "https://w2.example",
            "DATABRICKS_CLIENT_ID": "client",
            "DATABRICKS_CLIENT_SECRET": "secret",
        },
    )
    assert result.returncode == 0, result.stderr
    assert not id_file.exists()
    call_text = calls.read_text()
    assert "https://w1.example/oidc/v1/token" in call_text
    assert "https://w1.example/api/2.0/genie/spaces/space-123" in call_text
    assert "https://w2.example" not in call_text


def test_trash_mismatched_host_auth_failure_names_both_hosts(tmp_path):
    layer = tmp_path / "env"
    _write_layer_auth(layer, "https://w2.example")
    id_file = tmp_path / "space.id"
    id_file.write_text("space-123\n")
    curl = tmp_path / "curl"
    curl.write_text("#!/usr/bin/env bash\nprintf '{}\\n401\\n'\n")
    curl.chmod(0o755)
    result = subprocess.run(
        ["bash", str(SHARED / "scripts/genie_space.sh"), "trash"],
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "LAYER_ENV_DIR": str(layer),
            "GENIE_ID_FILE": str(id_file),
            "GENIE_EXPECTED_HOST": "https://w1.example",
            "DATABRICKS_HOST": "https://w2.example",
            "DATABRICKS_CLIENT_ID": "client",
            "DATABRICKS_CLIENT_SECRET": "secret",
        },
    )
    assert result.returncode != 0
    assert id_file.exists()
    assert "https://w1.example" in result.stderr
    assert "https://w2.example" in result.stderr


def test_trash_refuses_ambient_sp_without_layer_env(tmp_path):
    called = tmp_path / "curl-called"
    curl = tmp_path / "curl"
    curl.write_text(f"#!/bin/sh\ntouch {called}\nexit 99\n")
    curl.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DATABRICKS_HOST": "https://wrong.example",
        "DATABRICKS_CLIENT_ID": "ambient-id",
        "DATABRICKS_CLIENT_SECRET": "ambient-secret",
        "GENIE_ID_FILE": str(tmp_path / "space.id"),
    }
    env.pop("LAYER_ENV_DIR", None)
    result = subprocess.run(
        ["bash", str(SHARED / "scripts/genie_space.sh"), "trash"],
        text=True, capture_output=True, env=env,
    )
    assert result.returncode != 0
    assert "LAYER_ENV_DIR is required" in result.stderr
    assert not called.exists()


def _terraform_trigger_plan(tmp_path, changes):
    """Plan the production genie_space block (provisioners stripped) after a change."""
    source = (SHARED / "modules/workspace/main.tf").read_text()
    start = source.index('resource "terraform_data" "genie_space"')
    block = source[start: source.index("\n}\n", start) + 3]
    block = re.sub(r"\n  provisioner \"local-exec\" \{.*?\n  \}\n", "\n", block, flags=re.S)
    block = re.sub(r"\n  depends_on = \[.*?\n  \]\n", "\n", block, flags=re.S)
    block = re.sub(r"  for_each = local\.[a-z_]+\n", "", block)
    block = block.replace("var.genie_destroy_script", '"bash ../../scripts/genie_space.sh"')
    block = block.replace("var.databricks_workspace_host", "var.host")
    block = block.replace("${var.genie_id_file_prefix}_${each.key}", "id")
    module = tmp_path / "trigger-plan"
    module.mkdir()
    (module / "main.tf").write_text(
        'variable "host" { type = string }\n'
        'variable "client_id" { type = string }\n'
        'variable "client_secret" {\n  type = string\n  sensitive = true\n}\n'
        + block
    )
    env = {**os.environ, "TF_IN_AUTOMATION": "1", "TF_DATA_DIR": str(tmp_path / ".terraform")}
    base = ["-var=host=https://w1", "-var=client_id=old", "-var=client_secret=old"]

    def run(*args):
        result = tf(module, *args, env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    tf_init(module, env=env)
    run("apply", "-auto-approve", "-input=false", *base)
    assert '"old"' not in (module / "terraform.tfstate").read_text()  # no credential in state
    args = {"host": "https://w1", "client_id": "old", "client_secret": "old", **changes}
    plan = module / "plan.bin"  # embeds prior state: owner-only, then deleted
    try:
        run("plan", "-input=false", f"-out={plan}", *[f"-var={key}={value}" for key, value in args.items()])
        plan.chmod(0o600)
        shown = run("show", "-json", str(plan))
    finally:
        plan.unlink(missing_ok=True)
    return json.loads(shown.stdout)["resource_changes"][0]["change"]["actions"]


def test_host_change_plans_genie_space_replacement(tmp_path):
    assert _terraform_trigger_plan(tmp_path, {"host": "https://w2"}) == ["delete", "create"]


def test_credential_change_does_not_plan_genie_space_replacement(tmp_path):
    assert _terraform_trigger_plan(
        tmp_path, {"client_id": "new", "client_secret": "new"}
    ) == ["no-op"]


def test_apply_fingerprint_includes_terraform_and_genie_code():
    makefile = (SHARED / "Makefile.shared").read_text()
    assert '$(SHARED_ROOT)/roots/$$layer' in makefile
    assert '$(SHARED_ROOT)/modules/$$layer' in makefile
    assert '$(SHARED_ROOT)/scripts/genie_space.sh' in makefile
    assert '$(SHARED_ROOT)/deploy_masking_functions.py' in makefile
    assert '$(SHARED_ROOT)/masking_sql_blocks.py' in makefile
    assert '$(SHARED_ROOT)/modules/data_access/normalize_masking_sql.py' in makefile
    assert '$(SHARED_ROOT)/sql_tokenizer.py' in makefile
    assert "-type f -name '*.tf'" in makefile
    assert "! -path '*/tests/*'" in makefile
    assert "! -path '*/.terraform/*'" in makefile


def test_roots_no_longer_forward_the_retired_flag():
    for path in ROOTS:
        assert not re.search(r"business_access_enabled\s*=\s*var\.", path.read_text()), path


def _module_suite(tmp_path, *, layer_env: bool):
    """terraform test of every modules/workspace tftest in a copy, the real genie_space.sh included.

    create_to_id_handoff's teardown destroys a created space, whose destroy-time
    provisioner runs ../../scripts/genie_space.sh trash: it loads the env auth
    file from LAYER_ENV_DIR (here a fake env) and finds the ID file the stub
    create wrote, with its adopted marker, through GENIERAILS_GENIE_ID_DIR, as
    terraform_layer.sh exports them. curl must never run.
    """
    module = shared_copy(tmp_path, stub_genie=False) / "modules" / "workspace"
    layer = tmp_path / "env"
    _write_layer_auth(layer, "https://example.invalid")
    called = tmp_path / "curl-called"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(f'#!/bin/sh\necho "$*" >> {called}\nexit 99\n')
    (bin_dir / "curl").chmod(0o755)
    env = tf_env(tmp_path, PATH=f"{bin_dir}:{os.environ['PATH']}")
    env.pop("LAYER_ENV_DIR", None)
    env.pop("GENIERAILS_GENIE_ID_DIR", None)
    if layer_env:
        env |= {"LAYER_ENV_DIR": str(layer), "GENIERAILS_GENIE_ID_DIR": str(module / "tests/.tmp/handoff")}
    tf_init(module, env=env)
    return module, called, tf(module, "test", "-no-color", env=env)


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform not installed")
def test_workspace_module_suite_passes_including_teardown(tmp_path):
    module, called, result = _module_suite(tmp_path, layer_env=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert re.search(r"Success! \d+ passed, 0 failed\.", result.stdout), result.stdout
    assert "left the following resources in state" not in output
    # The adopted space was unmanaged, not trashed: its ID file and marker are gone, no API call.
    assert not list((module / "tests/.tmp/handoff").glob(".genie_*"))
    assert not called.exists()


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform not installed")
def test_workspace_module_suite_teardown_still_refuses_without_layer_env(tmp_path):
    _module, called, result = _module_suite(tmp_path, layer_env=False)
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode != 0
    assert "LAYER_ENV_DIR is required to load the env auth file before trashing a Genie agent" in output
    assert not called.exists()
