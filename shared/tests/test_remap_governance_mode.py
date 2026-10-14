"""Promotion keeps the destination's own governance_mode.

Each env migrates to deterministic governance on its own, so the remap of
env.auto.tfvars keeps the destination's governance_mode, never copies the
source's and never drops it.
"""

import subprocess
import sys
from pathlib import Path

import hcl2
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "remap_env_config.py"
PROD_SP = "5a7e0000-0000-0000-0000-00000000beef"
# A deterministic env commits its own deployer SP (deployer_identity.py).
DETERMINISTIC_PROD = f'governance_mode = "deterministic"\ndeployer_principal_id = "{PROD_SP}"\n'


def _promote(tmp_path, source_mode, dest_text):
    source = tmp_path / "envs" / "dev"
    dest = tmp_path / "envs" / "prod"
    source.mkdir(parents=True)
    mode_line = f'governance_mode = "{source_mode}"\n' if source_mode else ""
    (source / "env.auto.tfvars").write_text(f'{mode_line}genie_spaces = []\nuc_tables = ["dev_catalog.s.t"]\n')
    if dest_text is not None:
        dest.mkdir(parents=True)
        (dest / "env.auto.tfvars").write_text(dest_text)
    result = subprocess.run([sys.executable, str(SCRIPT), str(source), str(dest), "dev_catalog=prod_catalog"],
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return hcl2.loads((dest / "env.auto.tfvars").read_text()), result.stdout


@pytest.mark.parametrize("source_mode", ["deterministic", "legacy", None])
def test_promote_into_a_deterministic_prod_leaves_it_deterministic(tmp_path, source_mode):
    cfg, out = _promote(tmp_path, source_mode, DETERMINISTIC_PROD)

    assert cfg["governance_mode"] == "deterministic"
    assert cfg["deployer_principal_id"] == PROD_SP
    assert "Preserved destination governance_mode='deterministic'" in out
    assert cfg["uc_tables"] == ["prod_catalog.s.t"]


# A legacy prod may set its ID before it opts in (legacy-migration.md).
@pytest.mark.parametrize("dest_text", [DETERMINISTIC_PROD, DETERMINISTIC_PROD.replace("deterministic", "legacy")])
def test_promotion_keeps_the_destination_deployer_and_never_copies_the_source_one(tmp_path, dest_text):
    source, dest = tmp_path / "envs" / "dev", tmp_path / "envs" / "prod"
    source.mkdir(parents=True)
    dest.mkdir(parents=True)
    (source / "env.auto.tfvars").write_text('governance_mode = "deterministic"\n'
                                            'deployer_principal_id = "82bb9d6d-0000-0000-0000-000000000001"\n'
                                            'genie_spaces = []\nuc_tables = ["dev_catalog.s.t"]\n')
    (dest / "env.auto.tfvars").write_text(dest_text)

    result = subprocess.run([sys.executable, str(SCRIPT), str(source), str(dest), "dev_catalog=prod_catalog"],
                            text=True, capture_output=True, timeout=60)

    assert result.returncode == 0, result.stdout + result.stderr
    cfg = hcl2.loads((dest / "env.auto.tfvars").read_text())
    assert cfg.get("deployer_principal_id") == hcl2.loads(dest_text).get("deployer_principal_id")


@pytest.mark.parametrize("deployer", ["", 'deployer_principal_id = "account users"\n'])
def test_deterministic_prod_without_a_valid_deployer_is_refused_before_writing(tmp_path, deployer):
    source, dest = tmp_path / "envs" / "dev", tmp_path / "envs" / "prod"
    source.mkdir(parents=True)
    dest.mkdir(parents=True)
    (source / "env.auto.tfvars").write_text('genie_spaces = []\nuc_tables = ["dev_catalog.s.t"]\n')
    text = 'governance_mode = "deterministic"\n' + deployer
    (dest / "env.auto.tfvars").write_text(text)

    result = subprocess.run([sys.executable, str(SCRIPT), str(source), str(dest), "dev_catalog=prod_catalog"],
                            text=True, capture_output=True, timeout=60)

    assert result.returncode == 1
    assert "deployer_principal_id" in result.stdout and "nothing was written" in result.stdout
    assert (dest / "env.auto.tfvars").read_text() == text


@pytest.mark.parametrize("source_mode", ["deterministic", "legacy", None])
def test_promote_into_an_explicitly_legacy_prod_leaves_it_legacy(tmp_path, source_mode):
    cfg, _ = _promote(tmp_path, source_mode, 'governance_mode = "legacy"\n')

    assert cfg["governance_mode"] == "legacy"


@pytest.mark.parametrize("dest_text", ["sql_warehouse_id = \"w\"\n", None])
def test_promote_into_a_prod_without_a_mode_never_copies_the_source_mode(tmp_path, dest_text):
    # No line means legacy; a deterministic dev must not switch prod over.
    cfg, out = _promote(tmp_path, "deterministic", dest_text)

    assert "governance_mode" not in cfg
    assert "governance_mode" not in out


# ── acl_groups for an existing destination agent (live AWS Phase A, N4) ─────

SOURCE_AGENT = ('governance_mode = "deterministic"\n'
                'genie_spaces = [{ name = "Payments", genie_space_id = "dev1", uc_tables = ["dev_catalog.s.t"], '
                'acl_groups = ["dev_ops", "dev_viewers"] }]\nuc_tables = ["dev_catalog.s.t"]\n')
DEST_AGENT = 'genie_spaces = [{ name = "Payments", genie_space_id = "prod1", uc_tables = ["prod_catalog.s.t"]{acl} }]\n'


def _promote_agent(tmp_path, dest_text):
    source = tmp_path / "envs" / "dev"
    dest = tmp_path / "envs" / "prod"
    source.mkdir(parents=True)
    dest.mkdir(parents=True)
    (source / "env.auto.tfvars").write_text(SOURCE_AGENT)
    (dest / "env.auto.tfvars").write_text(dest_text)
    result = subprocess.run([sys.executable, str(SCRIPT), str(source), str(dest), "dev_catalog=prod_catalog"],
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return hcl2.loads((dest / "env.auto.tfvars").read_text()), result.stdout


@pytest.mark.parametrize("acl", ["", ", acl_groups = null"])
def test_first_deterministic_promotion_seeds_a_missing_agent_acl_through_group_map(tmp_path, acl):
    cfg, out = _promote_agent(tmp_path, DETERMINISTIC_PROD +
                              'group_map = { dev_ops = "prod_ops" }\n' + DEST_AGENT.replace("{acl}", acl))

    assert cfg["genie_spaces"][0]["acl_groups"] == ["prod_ops", "dev_viewers"]
    assert cfg["genie_spaces"][0]["genie_space_id"] == "prod1"
    assert "Seeded destination Genie space 'Payments' acl_groups=['prod_ops', 'dev_viewers'] from dev" in out


@pytest.mark.parametrize("acl, kept", [(', acl_groups = []', []), (', acl_groups = ["prod_ops"]', ["prod_ops"])])
def test_deterministic_promotion_never_overwrites_the_destination_acl(tmp_path, acl, kept):
    cfg, out = _promote_agent(tmp_path, DETERMINISTIC_PROD +
                              'group_map = { dev_ops = "prod_ops" }\n' + DEST_AGENT.replace("{acl}", acl))

    assert cfg["genie_spaces"][0]["acl_groups"] == kept
    assert "Seeded destination Genie space" not in out
    assert f"Preserved destination Genie space 'Payments' acl_groups={kept!r}" in out


@pytest.mark.parametrize("mode", ["", 'governance_mode = "legacy"\n'])
def test_legacy_destination_keeps_omitting_the_agent_acl(tmp_path, mode):
    cfg, out = _promote_agent(tmp_path, mode + DEST_AGENT.replace("{acl}", ""))

    assert "acl_groups" not in cfg["genie_spaces"][0]
    assert "Seeded destination Genie space" not in out
    assert "omitted acl_groups (destination policy derivation remains authoritative)" in out
