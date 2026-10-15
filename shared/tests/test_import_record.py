"""scripts/import_record.py: what an import-only manifest may carry, and the SQL release runs for it."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import import_record
from tests import fake_uc

SCRIPT = Path(__file__).parents[1] / "scripts/import_existing.sh"
REBUILT = "ALTER TABLE `cat`.`sch`.`tbl` UNSET TAGS ('sensitivity')"
CLEANUP = {"kind": "cleanup", "layer": "data_access", "entity_type": "tables", "entity_name": "cat.sch.tbl",
           "tag_key": "sensitivity", "statement": REBUILT, "warehouse_id": "wh"}
TREATMENT_VARIANTS = ["gr_treatment", "GR_TREATMENT", "Gr_Treatment", "gr_TREATMENT"]


@pytest.fixture
def env_dir(tmp_path):
    (tmp_path / "env.auto.tfvars").write_text('governance_mode = "deterministic"\nsql_warehouse_id = "wh"\n')
    return tmp_path


def _cleanup(tag_key):
    return CLEANUP | {"entity_type": "columns", "entity_name": "cat.sch.tbl.ssn", "tag_key": tag_key,
                      "statement": f"ALTER TABLE `cat`.`sch`.`tbl` ALTER COLUMN `ssn` UNSET TAGS ('{tag_key}')"}


def _tag_import(tag_key):
    return {"kind": "import", "layer": "data_access",
            "address": f'module.data_access.databricks_entity_tag_assignment.assignments["columns|cat.sch.tbl.ssn|{tag_key}|redact"]',
            "id": f"columns,cat.sch.tbl.ssn,{tag_key}"}


@pytest.mark.parametrize("tag_key", TREATMENT_VARIANTS)
@pytest.mark.parametrize("record", [_cleanup, _tag_import], ids=["cleanup", "tag-import"])
def test_every_case_of_the_treatment_tag_is_refused(env_dir, record, tag_key):
    """Unity Catalog tag keys are case-insensitive: GR_TREATMENT is the treatment tag too."""
    with pytest.raises(import_record.Invalid, match="belong to the staged policy controller"):
        import_record.validate([record(tag_key)], env_dir)
    with pytest.raises(import_record.Invalid):
        import_record.unset_sql("columns", "cat.sch.tbl.ssn", tag_key)


def test_a_cleanup_runs_the_rebuilt_statement_never_the_stored_one(env_dir):
    """Even an entry whose stored text changed after the check: what reaches the SDK is unset_sql()."""
    entries = import_record.validate([dict(CLEANUP)], env_dir)
    entries[0]["statement"] = "GRANT SELECT ON CATALOG cat TO `account users`"
    executed = []

    def execute_statement(*, statement, warehouse_id, **_):
        executed.append((statement, warehouse_id))
        return SimpleNamespace(status=SimpleNamespace(state="SUCCEEDED"))

    failed = import_record.run_cleanups(entries, "wh", SimpleNamespace(execute_statement=execute_statement))
    assert failed == 0 and executed == [(REBUILT, "wh")]


def _apply(tmp_path, entries, name):
    data_access = tmp_path / name / "env" / "data_access"
    data_access.mkdir(parents=True)
    (data_access.parent / "env.auto.tfvars").write_text('governance_mode = "deterministic"\nsql_warehouse_id = "wh"\n')
    (data_access / "abac.auto.tfvars").write_text("tag_assignments = []\n")
    record = tmp_path / name / "imports.jsonl"
    record.write_text("".join(json.dumps(e) + "\n" for e in entries))
    fake = fake_uc.install(tmp_path / name, {"warehouses": [{"id": "wh", "name": "Shared", "state": "RUNNING"}]})
    result = subprocess.run(["bash", SCRIPT, "--skip-treatment-tags", f"--apply-recorded={record}"],
                            cwd=data_access, env=os.environ | fake, text=True, capture_output=True)
    return result, fake


def test_a_stored_statement_other_than_the_rebuilt_one_is_refused_and_only_the_rebuilt_one_runs(tmp_path):
    # The same UNSET, unquoted (what d47351c recorded): not the rebuilt text, so nothing runs.
    stale = CLEANUP | {"statement": "ALTER TABLE cat.sch.tbl UNSET TAGS ('sensitivity')"}
    refused, fake = _apply(tmp_path, [stale], "stale")
    assert refused.returncode == 3 and "is not the UNSET the recorder writes" in refused.stderr
    assert fake_uc.events(fake) == []
    ran, fake = _apply(tmp_path, [CLEANUP], "rebuilt")
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert [(e["statement"], e["warehouse_id"]) for e in fake_uc.events(fake) if e["src"] == "sql"] == [(REBUILT, "wh")]
