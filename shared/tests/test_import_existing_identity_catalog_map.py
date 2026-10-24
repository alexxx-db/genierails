"""import_existing.sh and terraform_layer.sh, run by hand, refuse an identity catalog_map (S2).

Both are documented for direct use (shared/IMPORT_EXISTING.md, troubleshooting),
so make's guard isn't in front of them. A promoted map such as paycat=paycat whose
allow_identity_catalog_map = true was later removed must not let them import the
source env's policies by name: they refuse before any SQL, runner or Terraform
call and write nothing. The env's own HCL-boolean opt-in still lets them run.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests import fake_uc
from tests.test_import_existing import LIVE, STALE_TREATMENT

SHARED = Path(__file__).parents[1]
IMPORTER = SHARED / "scripts/import_existing.sh"
RUNNER = SHARED / "scripts/terraform_layer.sh"
IDENTITY = 'catalog_map = { "paycat" = "paycat" }\n'
OPT_IN = "allow_identity_catalog_map = true\n"
REFUSAL = "maps a catalog to itself (paycat=paycat)"


def _env(tmp_path, env_text):
    envs = tmp_path / "aws" / "envs"
    prod = envs / "prod"
    (prod / "data_access").mkdir(parents=True)
    (prod / "env.auto.tfvars").write_text(env_text)
    (prod / "abac.auto.tfvars").write_text(STALE_TREATMENT)
    (prod / "data_access" / "abac.auto.tfvars").write_text(STALE_TREATMENT)
    return envs, prod


def _snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _run_importer(cwd, fake, *args):
    environ = {k: v for k, v in os.environ.items() if k != "ENVS_DIR"} | fake
    return subprocess.run(["bash", IMPORTER, *args], cwd=cwd, env=environ, text=True, capture_output=True)


FORMS = [
    ("data_access", ()),
    ("data_access", ("--fgac-only",)),
    ("data_access", ("--tag-assignments-only",)),
    ("data_access", ("--grants-only",)),
    ("data_access", ("--dry-run",)),
    ("data_access", ("--fgac-only", "--dry-run")),
    ("data_access", ("--skip-treatment-tags", "--record={record}")),
    ("data_access", ("--skip-treatment-tags", "--apply-recorded={applied}")),
    ("prod", ()),
    ("prod", ("--dry-run",)),
]


@pytest.mark.parametrize(("where", "args"), FORMS, ids=[f"{w}:{' '.join(a) or 'all'}" for w, a in FORMS])
def test_import_existing_refuses_an_identity_map_before_any_call_or_write(tmp_path, where, args):
    envs, prod = _env(tmp_path, 'governance_mode = "legacy"\nsql_warehouse_id = "wh"\n' + IDENTITY)
    fake = fake_uc.install(tmp_path, LIVE)
    applied = tmp_path / "applied.jsonl"
    applied.write_text(json.dumps({"kind": "import", "layer": "data_access", "address": "a", "id": "i"}) + "\n")
    record = tmp_path / "record.jsonl"
    before = _snapshot(envs)

    result = _run_importer(prod / "data_access" if where == "data_access" else prod, fake,
                           *(a.format(record=record, applied=applied) for a in args))

    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert f"catalog_map in envs/prod/env.auto.tfvars {REFUSAL}" in out
    assert "set allow_identity_catalog_map = true in envs/prod/env.auto.tfvars" in out
    assert fake_uc.events(fake) == []  # no SQL, no runner call
    assert _snapshot(envs) == before and not record.exists()


def test_import_existing_with_the_env_opt_in_imports(tmp_path):
    envs, prod = _env(tmp_path, 'sql_warehouse_id = "wh"\n' + IDENTITY + OPT_IN)
    fake = fake_uc.install(tmp_path, LIVE)

    result = _run_importer(prod / "data_access", fake, "--tag-assignments-only")

    assert result.returncode == 0, result.stdout + result.stderr
    assert REFUSAL not in result.stdout + result.stderr
    assert fake_uc.runner_imports(fake)
    record = tmp_path / "record.jsonl"
    result = _run_importer(prod / "data_access", fake, "--fgac-only", "--skip-treatment-tags", f"--record={record}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert record.exists()


def test_import_existing_ignores_a_string_opt_in_or_one_in_a_layer_local_file(tmp_path):
    envs, prod = _env(tmp_path, 'sql_warehouse_id = "wh"\n' + IDENTITY + 'allow_identity_catalog_map = "true"\n')
    (prod / "data_access" / "env.auto.tfvars").write_text(IDENTITY + OPT_IN)  # never the env's file
    fake = fake_uc.install(tmp_path, LIVE)

    result = _run_importer(prod / "data_access", fake, "--fgac-only")

    assert result.returncode == 1 and REFUSAL in result.stdout + result.stderr
    assert fake_uc.events(fake) == []


def _run_runner(tmp_path, envs, *argv):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "terraform.log"
    (bin_dir / "terraform").write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit 0\n')
    (bin_dir / "terraform").chmod(0o755)
    environ = {k: v for k, v in os.environ.items() if k not in ("LAYER_ENV_DIR", "CI")}
    environ |= {"ENVS_DIR": str(envs), "PATH": f"{bin_dir}:{environ['PATH']}"}
    result = subprocess.run(["bash", RUNNER, *argv], env=environ, text=True, capture_output=True)
    return result, (log.read_text() if log.exists() else "")


RUNNER_CALLS = [
    ("data_access", "import", "module.data_access.databricks_policy_info.policies[\"x\"]", "id"),
    ("data_access", "apply"),
    ("data_access", "apply-plan", "saved.tfplan"),
    ("data_access", "destroy"),
    ("workspace", "import", "module.workspace.x", "id"),
    ("workspace", "apply"),
]


@pytest.mark.parametrize("call", RUNNER_CALLS, ids=[f"{c[0]}:{c[1]}" for c in RUNNER_CALLS])
def test_the_layer_runner_refuses_state_writes_for_an_identity_map(tmp_path, call):
    envs, prod = _env(tmp_path, 'governance_mode = "legacy"\n' + IDENTITY)
    before = _snapshot(envs)

    result, terraform = _run_runner(tmp_path, envs, call[0], "prod", *call[1:])

    assert result.returncode == 1, result.stdout + result.stderr
    assert REFUSAL in result.stderr
    assert terraform == "" and _snapshot(envs) == before


def test_the_layer_runner_passes_with_the_opt_in_and_for_read_only_commands(tmp_path):
    envs, prod = _env(tmp_path, 'governance_mode = "legacy"\n' + IDENTITY + OPT_IN)
    result, _ = _run_runner(tmp_path, envs, "data_access", "prod", "import", "module.data_access.x", "id")
    assert REFUSAL not in result.stdout + result.stderr
    (prod / "env.auto.tfvars").write_text('governance_mode = "legacy"\n' + IDENTITY)
    result, _ = _run_runner(tmp_path, envs, "data_access", "prod", "state-list")
    assert REFUSAL not in result.stdout + result.stderr
