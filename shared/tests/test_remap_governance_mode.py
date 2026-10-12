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
    cfg, out = _promote(tmp_path, source_mode, 'governance_mode = "deterministic"\n')

    assert cfg["governance_mode"] == "deterministic"
    assert "Preserved destination governance_mode='deterministic'" in out
    assert cfg["uc_tables"] == ["prod_catalog.s.t"]


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
