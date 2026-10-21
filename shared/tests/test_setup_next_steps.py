"""Regression tests for `make setup` next steps (dev_to_prod champion flow)."""

import os
import re
import subprocess
import sys
from pathlib import Path

import hcl2
import pytest


ROOT = Path(__file__).parents[2]
SHARED_ROOT = ROOT / "shared"
CLOUDS = ("aws", "azure")


def _clean_env():
    return {
        key: value
        for key, value in os.environ.items()
        if key not in ("MAKEFLAGS", "MAKELEVEL", "ENV", "ACCOUNT_ADMIN_ENV")
    }


def _make(cloud, tmp_path, *args):
    cloud_root = tmp_path / cloud
    cloud_root.mkdir(exist_ok=True)
    return subprocess.run(
        [
            "make",
            "--no-print-directory",
            *args,
            f"CLOUD_ROOT={cloud_root}",
            f"SHARED_ROOT={SHARED_ROOT}",
        ],
        cwd=ROOT / cloud,
        text=True,
        capture_output=True,
        env=_clean_env(),
    )


def _defined_targets():
    makefile = (SHARED_ROOT / "Makefile.shared").read_text()
    return {
        line.split(":", 1)[0]
        for line in makefile.splitlines()
        if line and not line[0].isspace() and ":" in line and "=" not in line.split(":", 1)[0]
    }


def _mentioned_targets(output):
    # Commands appear as "Run: make <t>", "or: make <t>", or indented on their own line.
    return re.findall(r"(?:^\s+|: )make ([a-z][a-z-]*)", output, flags=re.MULTILINE)


@pytest.mark.parametrize("cloud", CLOUDS)
def test_setup_dev_prints_champion_phase_1_steps(cloud, tmp_path):
    result = _make(cloud, tmp_path, "setup", "ENV=dev")
    assert result.returncode == 0, result.stderr
    out = result.stdout

    assert "cp ../shared/examples/dev_to_prod/env.auto.tfvars.example envs/dev/env.auto.tfvars" in out
    assert "envs/dev/auth.auto.tfvars" in out
    assert "manage_groups" not in out
    assert "Edit envs/account/env.auto.tfvars" not in out
    # Each agent gets its own file with its ID and who may run it (decision 4).
    assert (
        "  3. Per agent, create its file (the file name is the agent's name) and set genie_space_id to its\n"
        "     Genie agent ID and acl_groups to the groups that may run it ([] means nobody; required):\n"
        "       mkdir -p envs/dev/agents\n"
        '       cp ../shared/examples/dev_to_prod/agent.auto.tfvars.example "envs/dev/agents/<agent name>.auto.tfvars"\n'
        "     In envs/dev/env.auto.tfvars set deployer_principal_id to the applying SP's client_id, and\n"
        "     access_tier_groups to your IdP groups, most to least privileged (the template starts new champions in deterministic mode)\n"
    ) in out
    # One plain generate imports the agent; no separate MODE=genie step.
    assert "MODE=genie" not in out
    # access_tier_groups makes --groups unnecessary on every champion command.
    assert "GENERATE_ARGS" not in out
    assert "--groups" not in out
    assert "     (No agent yet? See ../shared/examples/dev_to_prod/SAMPLE_ENV.md)\n" in out
    # verify-access picks the key itself; setup never asks for one.
    assert "     verify-access picks a row-pairing key per masked table; the proven keys are saved and promoted.\n" in out
    assert "VERIFY_KEY_COLUMN" not in out and "verify_key_column" not in out
    # The champion flow needs only the agent ID; tables are discovered.
    assert "uc_tables" not in out
    assert "existing agent:" not in out
    assert "pick one" not in out
    assert "  4. In Catalog Explorer, open the catalog > Details tab > Data classification: turn it on.\n" in out
    assert "     When the scan finishes, review the detections and exclude any false positives, then turn on\n" in out
    assert "     auto-tagging. Wait until the class.* tags appear.\n" in out
    assert "     Prefer a script? make enable-classification ENV=dev turns it on (you still review detections in the UI).\n" in out
    assert "  5. Run: make generate ENV=dev   (one run: imports the agent, finds its tables, drafts rules)\n" in out
    assert "  6. Run: make rehearse ENV=dev\n" in out
    assert "business_access_enabled" not in out
    assert "     (grants SELECT only from table_readers after its coverage check passes, and Genie CAN_RUN only from acl_groups after the live agent coverage check)\n" in out
    assert re.findall(r"^  (\d+)\. ", out, flags=re.MULTILINE) == ["1", "2", "3", "4", "5", "6"]
    assert "shared/examples/dev_to_prod/README.md" in out
    # The champion flow relies on native Data Classification, not the country overlay.
    assert "APJ" not in out
    assert "country" not in out
    # Setup points dev at rehearse (derive, validate and gate first), not plain apply.
    assert "make apply" not in out
    assert (
        out.index("envs/dev/auth.auto.tfvars")
        < out.index("set genie_space_id")
        < out.index("access_tier_groups")
        < out.index("SAMPLE_ENV.md")
        < out.index("enable-classification")
        < out.rindex("make generate")
        < out.index("make rehearse")
    )


@pytest.mark.parametrize("cloud", CLOUDS)
def test_setup_omits_completed_walkthrough_copy_and_renumbers(cloud, tmp_path):
    first = _make(cloud, tmp_path, "setup", "ENV=dev")
    assert first.returncode == 0
    env_file = tmp_path / cloud / "envs/dev/env.auto.tfvars"
    env_file.write_text((SHARED_ROOT / "examples/dev_to_prod/env.auto.tfvars.example").read_text())

    result = _make(cloud, tmp_path, "setup", "ENV=dev")
    assert result.returncode == 0, result.stderr
    assert "cp ../shared/examples/dev_to_prod/env.auto.tfvars.example" not in result.stdout
    assert re.findall(r"^  (\d+)\. ", result.stdout, flags=re.MULTILINE) == ["1", "2", "3", "4", "5"]


def test_setup_existing_legacy_env_does_not_request_a_deployer_principal(tmp_path):
    cloud_root = tmp_path / "aws"
    env_dir = cloud_root / "envs" / "dev"
    env_dir.mkdir(parents=True)
    (env_dir / "env.auto.tfvars").write_text('governance_mode = "legacy"\n')

    result = _make("aws", tmp_path, "setup", "ENV=dev")

    assert result.returncode == 0, result.stderr
    assert "deployer_principal_id" not in result.stdout


@pytest.mark.parametrize("cloud", CLOUDS)
def test_setup_prod_prints_promote_release_maintain_steps(cloud, tmp_path):
    result = _make(cloud, tmp_path, "setup", "ENV=prod")
    assert result.returncode == 0, result.stderr
    out = result.stdout

    assert "Next steps (prod promotion — the walkthrough's Prod section; finish the source rehearsal first):" in out
    order = [
        "Replace the seeded promote_from and catalog_map values in envs/prod/env.auto.tfvars",
        'make promote-to ENV=prod\n',
        "     (FROM=/CATALOG_MAP= remain optional command-line overrides.)\n",
        "envs/prod/auth.auto.tfvars",
        "In Catalog Explorer, open the catalog > Details tab > Data classification: turn it on",
        "Prefer a script? make enable-classification ENV=prod turns it on",
        "  5. Commit and push .gitignore and envs/ (git add .gitignore envs/) to origin/main; on that commit run make rehearse ENV=dev",
        "     make release-plan ENV=prod   (checks the commit was rehearsed",
        "Get that manifest approved, then: make release ENV=prod\n",
        "make maintain ENV=prod",
    ]
    positions = [out.index(step) for step in order]
    assert positions == sorted(positions)
    # release-plan needs a dev rehearsal of the promotion commit; prod itself never rehearses.
    assert "make rehearse ENV=prod" not in out
    assert "make generate" not in out
    assert "deployer_principal_id" in out
    assert "VERIFY_KEY_COLUMN" not in out
    assert "DEST_CATALOG_MAP" not in out
    assert "     It proves a row-pairing key for every masked table before applying (no key to set).\n" in out
    assert "VERIFY_KEY_COLUMN" not in out and "verify_key_column" not in out
    assert "Set business_access_enabled" not in out
    assert "business_access_enabled" not in out
    assert "     (grants SELECT only from table_readers after its coverage check passes, and Genie CAN_RUN only from acl_groups after the live agent coverage check)\n" in out
    assert "make apply ENV=prod" not in out
    assert "make verify-access ENV=prod" not in out
    assert "make certify" not in out
    assert re.findall(r"^  (\d+)\. ", out, flags=re.MULTILINE) == ["1", "2", "3", "4", "5", "6"]
    assert "shared/examples/dev_to_prod/README.md" in out
    env_text = (tmp_path / cloud / "envs/prod/env.auto.tfvars").read_text()
    env_cfg = hcl2.loads(env_text)
    assert env_cfg["governance_mode"] == "deterministic"
    assert env_cfg["deployer_principal_id"] == "<your-deployer-principal-id>"
    assert env_cfg["access_tier_groups"] == ["<most-privileged-group>", "<least-privileged-group>"]
    assert "enable_auto_tagging" not in hcl2.loads(env_text)
    assert 'promote_from = "dev"' in env_text
    assert 'catalog_map  = { "<dev_catalog>" = "<prod_catalog>" }' in env_text


def test_setup_prod_rehearses_its_promote_from_source_before_release_plan(tmp_path):
    """release-plan needs a same-commit receipt from promote_from's env, not always dev."""
    assert _make("aws", tmp_path, "setup", "ENV=prod").returncode == 0
    env_file = tmp_path / "aws" / "envs" / "prod" / "env.auto.tfvars"
    text = env_file.read_text()
    assert 'promote_from = "dev"' in text
    env_file.write_text(text.replace('promote_from = "dev"', 'promote_from = "stg"'))

    result = _make("aws", tmp_path, "setup", "ENV=prod")

    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "  5. Commit and push .gitignore and envs/ (git add .gitignore envs/) to origin/main; on that commit run make rehearse ENV=stg" in out
    assert "make rehearse ENV=dev" not in out
    assert out.index("make rehearse ENV=stg") < out.index("make release-plan ENV=prod")


def test_setup_prod_banner_uses_legacy_promote_from_mode(tmp_path):
    dev = tmp_path / "aws/envs/dev"
    dev.mkdir(parents=True)
    dev.joinpath("env.auto.tfvars").write_text('governance_mode = "legacy"\n')
    result = _make("aws", tmp_path, "setup", "ENV=prod")
    assert result.returncode == 0, result.stderr
    assert "  5. Run: make release ENV=prod" in result.stdout
    assert "make rehearse ENV=dev" not in result.stdout
    assert "make release-plan ENV=prod" not in result.stdout
    assert 'migrate envs/dev (shared/docs/legacy-migration.md), or set governance_mode = "legacy"' in result.stdout


@pytest.mark.parametrize("env", ["prod", "stg"])
def test_setup_legacy_promoted_env_keeps_the_promotion_banner(env, tmp_path):
    dev = tmp_path / "aws/envs/dev"
    target = tmp_path / f"aws/envs/{env}"
    dev.mkdir(parents=True)
    target.mkdir()
    dev.joinpath("env.auto.tfvars").write_text('governance_mode = "legacy"\n')
    target.joinpath("env.auto.tfvars").write_text(
        'governance_mode = "legacy"\npromote_from = "dev"\n'
        f'catalog_map = {{ "dev_catalog" = "{env}_catalog" }}\n')
    result = _make("aws", tmp_path, "setup", f"ENV={env}")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert f"Next steps ({env} promotion" in out
    assert f"{env} workspace host & id" in out
    assert f"make promote-to ENV={env}" in out
    assert f"make release ENV={env}" in out
    assert f"make rehearse ENV={env}" not in out
    assert "make release-plan" not in out
    assert "cp ../shared/examples/dev_to_prod/env.auto.tfvars.example" not in out


@pytest.mark.parametrize("cloud", CLOUDS)
def test_setup_non_default_env_seeds_promotion_settings(cloud, tmp_path):
    result = _make(cloud, tmp_path, "setup", "ENV=stg")
    assert result.returncode == 0, result.stderr
    env_text = (tmp_path / cloud / "envs/stg/env.auto.tfvars").read_text()
    assert 'promote_from = "dev"' in env_text
    assert 'catalog_map  = { "<dev_catalog>" = "<stg_catalog>" }' in env_text


def test_setup_stg_promoted_uses_the_promotion_banner_without_overwrite_or_rehearse(tmp_path):
    result = _make("aws", tmp_path, "setup", "ENV=stg")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "Next steps (stg promotion" in out
    assert "stg workspace host & id" in out
    assert "prod workspace host & id" not in out
    assert "make promote-to ENV=stg" in out
    assert "make rehearse ENV=dev" in out
    assert "make rehearse ENV=stg" not in out
    assert "cp ../shared/examples/dev_to_prod/env.auto.tfvars.example envs/stg/env.auto.tfvars" not in out


@pytest.mark.parametrize("env", ["prod", "stg"])
def test_setup_promoted_env_says_set_group_map_and_keeps_the_tier_placeholders(env, tmp_path):
    """promote-to writes a promoted env's tiers from its source's through group_map."""
    result = _make("aws", tmp_path, "setup", f"ENV={env}")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert (f"     Where {env}'s group names differ from dev's, set group_map there (each dev group = its {env} "
            "group): promote-to writes this env's access_tier_groups from dev's through it, so leave those "
            "placeholders.\n") in out, out
    assert "Replace this env's access_tier_groups placeholders" not in out
    assert "access_tier_groups placeholder" not in out  # no "Setup required" advice to replace them
    cfg = hcl2.loads((tmp_path / "aws" / "envs" / env / "env.auto.tfvars").read_text())
    assert cfg["access_tier_groups"] == ["<most-privileged-group>", "<least-privileged-group>"]


def test_setup_stg_without_promote_from_uses_the_rehearsal_banner(tmp_path):
    env = tmp_path / "aws/envs/stg"
    env.mkdir(parents=True)
    env.joinpath("env.auto.tfvars").write_text('governance_mode = "deterministic"\n')
    result = _make("aws", tmp_path, "setup", "ENV=stg")
    assert result.returncode == 0, result.stderr
    assert "Next steps (dev rehearsal" in result.stdout
    assert "make rehearse ENV=stg" in result.stdout
    assert "promotion — the walkthrough's Prod section" not in result.stdout


@pytest.mark.parametrize("target", ["promote-to", "check"])
@pytest.mark.parametrize("ci", [False, True])
def test_duplicate_catalog_map_is_actionable_without_parser_traceback(target, ci, tmp_path):
    assert _make("aws", tmp_path, "setup", "ENV=prod").returncode == 0
    env_file = tmp_path / "aws/envs/prod/env.auto.tfvars"
    env_file.write_text(env_file.read_text() + '\ncatalog_map = { "other" = "prod" }\n')
    args = [target, "ENV=prod"] + (["CI=true"] if ci else [])
    result = _make("aws", tmp_path, *args)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "envs/prod/env.auto.tfvars: catalog_map is set twice; keep one" in output
    assert "Traceback" not in output and "VisitError" not in output


def test_duplicate_catalog_map_helpers_return_one(tmp_path):
    envs = tmp_path / "envs"
    dev, prod = envs / "dev", envs / "prod"
    dev.mkdir(parents=True)
    prod.mkdir()
    dev.joinpath("env.auto.tfvars").write_text('uc_tables = ["dev.s.t"]\n')
    duplicate = ('governance_mode = "deterministic"\npromote_from = "dev"\n'
                 'catalog_map = { "dev" = "prod" }\ncatalog_map = { "other" = "prod" }\n')
    prod_file = prod / "env.auto.tfvars"
    prod_file.write_text(duplicate)
    commands = [
        [sys.executable, str(SHARED_ROOT / "genie_space_placeholder.py"), str(prod_file)],
        [sys.executable, str(SHARED_ROOT / "scripts/saved_settings.py"), "promote-resolve",
         "--env", "prod", "--env-dir", str(prod), "--envs-dir", str(envs)],
    ]
    for command in commands:
        result = subprocess.run(command, text=True, capture_output=True, env=_clean_env())
        assert result.returncode == 1
        assert "catalog_map is set twice; keep one" in result.stderr
        assert "Traceback" not in result.stderr and "VisitError" not in result.stderr


@pytest.mark.parametrize("env", ["dev", "prod", "account"])
def test_setup_mentions_only_existing_make_targets(env, tmp_path):
    result = _make("aws", tmp_path, "setup", f"ENV={env}")
    assert result.returncode == 0, result.stderr
    mentioned = _mentioned_targets(result.stdout)
    assert mentioned
    assert set(mentioned) <= _defined_targets()


def test_setup_does_not_create_account_admin_env(tmp_path):
    admin_env = tmp_path / "account-admin.aws.env"
    result = _make("aws", tmp_path, "setup", "ENV=dev", f"ACCOUNT_ADMIN_ENV={admin_env}")
    assert result.returncode == 0, result.stderr
    assert not admin_env.exists()
    assert "account-admin" not in result.stdout
    assert "test-ci" not in result.stdout


def test_test_ci_guard_rejects_missing_custom_account_admin_env(tmp_path):
    admin_env = tmp_path / "missing.env"
    result = _make("aws", tmp_path, "test-ci", f"ACCOUNT_ADMIN_ENV={admin_env}")
    assert result.returncode != 0
    assert f"ACCOUNT_ADMIN_ENV file not found: {admin_env}" in result.stdout
    assert "CI Pipeline" not in result.stdout
    assert not admin_env.exists()


def test_test_ci_guard_creates_default_account_admin_env_and_stops(tmp_path):
    admin_env = tmp_path / "account-admin.aws.env"
    result = _make(
        "aws",
        tmp_path,
        "test-ci",
        f"_DEFAULT_ACCOUNT_ADMIN_ENV={admin_env}",
        f"ACCOUNT_ADMIN_ENV={admin_env}",
    )
    assert result.returncode != 0
    assert f"Created {admin_env}" in result.stdout
    assert "CI Pipeline" not in result.stdout
    assert admin_env.read_text() == (SHARED_ROOT / "scripts" / "account-admin.aws.env.example").read_text()


def test_cross_env_promote_points_to_release_not_apply():
    makefile = (SHARED_ROOT / "Makefile.shared").read_text()
    block = makefile[makefile.index("=== Promote complete:"):]
    block = block[:block.index("trap - EXIT")]

    # release-steps prints release-plan and release after a same-commit source rehearsal, or a legacy release.
    assert '$(_RELEASE_STEPS) --env-dir "$$dest_env_dir" --env "$(DEST_ENV)"' in block
    assert "make apply ENV=$(DEST_ENV)" not in block
