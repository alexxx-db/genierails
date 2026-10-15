import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import fake_uc


SCRIPT = Path(__file__).parents[1] / "scripts/import_existing.sh"


def _cleanup_python() -> str:
    source = SCRIPT.read_text()
    match = re.search(
        r"cleanup_stale_tag_assignments\(\) \{\n  python3 - << 'PYEOF'\n(.*?)\nPYEOF\n\}",
        source,
        re.DOTALL,
    )
    assert match
    return match.group(1)


class _State:
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


def _response(state, value=None, statement_id="lookup"):
    rows = [] if value is None else [[value]]
    return SimpleNamespace(
        statement_id=statement_id,
        status=SimpleNamespace(state=state, error=None),
        result=SimpleNamespace(data_array=rows),
    )


@pytest.mark.parametrize(
    ("initial", "polled", "lookup_raises", "poll_raises", "expected_unsets"),
    [
        (_response(_State.SUCCEEDED, "pii"), None, False, False, 0),
        (_response(_State.SUCCEEDED, "wrong"), None, False, False, 1),
        (_response(_State.FAILED), None, False, False, 1),
        (_response(_State.PENDING), _response(_State.SUCCEEDED), False, False, 1),
        (None, None, True, False, 1),
        (_response(_State.PENDING), None, False, True, 1),
        (_response(_State.PENDING), _response(_State.PENDING), False, False, 1),
        (_response(_State.PENDING), _response(_State.SUCCEEDED, "pii"), False, False, 0),
    ],
    ids=[
        "success-match",
        "success-mismatch",
        "failed",
        "pending-without-row",
        "lookup-raises",
        "poll-raises",
        "poll-timeout",
        "pending-to-success-match",
    ],
)
def test_stale_tag_lookup_fails_closed(
    monkeypatch, tmp_path, initial, polled, lookup_raises, poll_raises, expected_unsets
):
    (tmp_path / "abac.auto.tfvars").write_text(
        'tag_assignments = [{ entity_type = "tables", entity_name = "cat.sch.tbl", '
        'tag_key = "sensitivity", tag_value = "pii" }]\n'
    )
    (tmp_path / "env.auto.tfvars").write_text('sql_warehouse_id = "wh"\n')

    calls = []

    class Execution:
        def execute_statement(self, *, statement, **_kwargs):
            calls.append(statement)
            if statement.startswith("SELECT"):
                if lookup_raises:
                    raise RuntimeError("lookup failed")
                return initial
            return _response(_State.SUCCEEDED, statement_id="unset")

        def get_statement(self, _statement_id):
            if poll_raises:
                raise RuntimeError("poll failed")
            assert polled is not None
            return polled

    client = SimpleNamespace(statement_execution=Execution())
    sdk = types.ModuleType("databricks.sdk")
    sdk.WorkspaceClient = lambda **_kwargs: client
    sql = types.ModuleType("databricks.sdk.service.sql")
    sql.StatementState = _State
    monkeypatch.setitem(sys.modules, "databricks.sdk", sdk)
    monkeypatch.setitem(sys.modules, "databricks.sdk.service.sql", sql)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    exec(compile(_cleanup_python(), str(SCRIPT), "exec"), {"__name__": "__main__"})

    assert sum(statement.startswith("ALTER TABLE") for statement in calls) == expected_unsets


def test_deterministic_cleanup_skips_treatments_but_cleans_other_tags(monkeypatch, tmp_path):
    monkeypatch.setenv("GENIERAILS_IMPORT_SKIP_TREATMENT_TAGS", "true")
    (tmp_path / "abac.auto.tfvars").write_text(
        'tag_assignments = [\n'
        '  { entity_type = "columns", entity_name = "cat.sch.tbl.ssn", tag_key = "gr_treatment", tag_value = "redact__string__12345678" },\n'
        '  { entity_type = "tables", entity_name = "cat.sch.tbl", tag_key = "sensitivity", tag_value = "pii" }\n'
        ']\n'
    )
    (tmp_path / "env.auto.tfvars").write_text(
        'governance_mode = "deterministic"\nsql_warehouse_id = "wh"\n'
    )
    calls = []

    class Execution:
        def execute_statement(self, *, statement, **_kwargs):
            calls.append(statement)
            if statement.startswith("SELECT"):
                return _response(_State.SUCCEEDED, "old")
            return _response(_State.SUCCEEDED, statement_id="unset")

    client = SimpleNamespace(statement_execution=Execution())
    sdk = types.ModuleType("databricks.sdk")
    sdk.WorkspaceClient = lambda **_kwargs: client
    sql = types.ModuleType("databricks.sdk.service.sql")
    sql.StatementState = _State
    monkeypatch.setitem(sys.modules, "databricks.sdk", sdk)
    monkeypatch.setitem(sys.modules, "databricks.sdk.service.sql", sql)
    monkeypatch.chdir(tmp_path)
    exec(compile(_cleanup_python(), str(SCRIPT), "exec"), {"__name__": "__main__"})

    alters = [statement for statement in calls if statement.startswith("ALTER TABLE")]
    assert alters == ["ALTER TABLE cat.sch.tbl UNSET TAGS ('sensitivity')"]


def test_deterministic_import_skips_treatments_including_steady_state_retag(tmp_path):
    data_access = tmp_path / "env" / "data_access"
    data_access.mkdir(parents=True)
    (tmp_path / "env" / "env.auto.tfvars").write_text('governance_mode = "deterministic"\n')
    (data_access / "abac.auto.tfvars").write_text(
        'tag_assignments = [\n'
        '  { entity_type = "columns", entity_name = "cat.sch.tbl.ssn", tag_key = "gr_treatment", tag_value = "redact__string__newvalue" },\n'
        '  { entity_type = "tables", entity_name = "cat.sch.tbl", tag_key = "sensitivity", tag_value = "pii" }\n'
        ']\n'
    )
    result = subprocess.run(
        ["bash", SCRIPT, "--tag-assignments-only", "--dry-run", "--skip-treatment-tags"], cwd=data_access,
        env=os.environ, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "gr_treatment" not in result.stdout
    assert 'assignments["tables|cat.sch.tbl|sensitivity|pii"]' in result.stdout


STALE_TREATMENT = (
    'tag_assignments = [\n'
    '  { entity_type = "columns", entity_name = "cat.sch.tbl.ssn", tag_key = "gr_treatment", tag_value = "redact__string__newvalue" },\n'
    '  { entity_type = "tables", entity_name = "cat.sch.tbl", tag_key = "sensitivity", tag_value = "pii" },\n'
    '  { entity_type = "tables", entity_name = "cat.sch.tbl", tag_key = "owner", tag_value = "fin" }\n'
    ']\n'
)
# Live UC: the treatment tag still has its legacy value, sensitivity is stale, owner is current.
LIVE = {
    "column_tags": [["cat.sch.tbl.ssn", "gr_treatment", "redact"]],
    "table_tags": [["cat.sch.tbl", "sensitivity", "public"], ["cat.sch.tbl", "owner", "fin"]],
    "warehouses": [{"id": "wh", "name": "Shared", "state": "RUNNING"}],
}


def _layer(tmp_path, env_file=None, abac=STALE_TREATMENT):
    data_access = tmp_path / "env" / "data_access"
    data_access.mkdir(parents=True)
    if env_file is not None:
        (tmp_path / "env" / "env.auto.tfvars").write_text(env_file)
    (data_access / "abac.auto.tfvars").write_text(abac)
    return data_access


def _run(data_access, fake, *args):
    return subprocess.run(["bash", SCRIPT, "--tag-assignments-only", *args], cwd=data_access,
                          env=os.environ | fake, text=True, capture_output=True)


@pytest.mark.parametrize("env_file", ["not hcl {\n", 'governance_mode = "determinstic"\n', None],
                         ids=["unparseable", "unknown-mode", "missing"])
def test_unreadable_governance_mode_stops_before_any_import_or_unset(tmp_path, env_file):
    data_access = _layer(tmp_path, env_file)
    fake = fake_uc.install(tmp_path, LIVE)
    result = _run(data_access, fake)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "cannot tell who owns gr_treatment tags" in result.stderr
    assert fake_uc.events(fake) == []  # no SQL, no runner call: nothing imported or cleared


def test_a_stale_layer_local_env_file_does_not_hide_a_deterministic_env(tmp_path):
    """The env-level file decides, never data_access/env.auto.tfvars (a stale copy, not the symlink)."""
    data_access = _layer(tmp_path, 'governance_mode = "deterministic"\n')
    (data_access / "env.auto.tfvars").write_text('governance_mode = "legacy"\nsql_warehouse_id = "wh"\n')
    fake = fake_uc.install(tmp_path, LIVE)
    result = _run(data_access, fake)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fake_uc.sql_writes(fake) == ["ALTER TABLE cat.sch.tbl UNSET TAGS ('sensitivity')"]
    imports = fake_uc.runner_imports(fake)
    assert imports and not [argv for argv in imports if "gr_treatment" in argv[3]]


def test_a_missing_governance_mode_is_legacy_as_everywhere_else(tmp_path):
    data_access = _layer(tmp_path, 'sql_warehouse_id = "wh"\n')
    fake = fake_uc.install(tmp_path, LIVE)
    result = _run(data_access, fake)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALTER TABLE cat.sch.tbl ALTER COLUMN ssn UNSET TAGS ('gr_treatment')" in fake_uc.sql_writes(fake)


def _state_bytes(data_access):
    path = data_access / "terraform.tfstate"
    return path.read_bytes() if path.exists() else None


def test_record_mode_changes_nothing_live_or_in_state(tmp_path):
    """release-plan: the stale tag and the importable one are recorded, never cleared or imported."""
    data_access = _layer(tmp_path, 'governance_mode = "deterministic"\nsql_warehouse_id = "wh"\n')
    fake = fake_uc.install(tmp_path, LIVE)
    record = tmp_path / "imports.jsonl"
    result = _run(data_access, fake, "--skip-treatment-tags", f"--record={record}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert fake_uc.sql_writes(fake) == [] and fake_uc.runner_imports(fake) == []
    assert _state_bytes(data_access) is None
    entries = [json.loads(line) for line in record.read_text().splitlines()]
    assert entries == [
        {"kind": "cleanup", "layer": "data_access", "entity_type": "tables", "entity_name": "cat.sch.tbl",
         "tag_key": "sensitivity", "statement": "ALTER TABLE cat.sch.tbl UNSET TAGS ('sensitivity')",
         "warehouse_id": "wh"},
        {"kind": "import", "layer": "data_access",
         "address": 'module.data_access.databricks_entity_tag_assignment.assignments["tables|cat.sch.tbl|owner|fin"]',
         "id": "tables,cat.sch.tbl,owner"},
    ]


def test_record_mode_refuses_what_it_cannot_verify(tmp_path):
    abac = ('fgac_policies = [{ name = "mask", catalog = "cat", policy_type = "POLICY_TYPE_COLUMN_MASK" }]\n')
    data_access = _layer(tmp_path, 'governance_mode = "deterministic"\n', abac)
    fake = fake_uc.install(tmp_path, LIVE | {"rest_down": True})
    result = subprocess.run(["bash", SCRIPT, "--fgac-only", "--skip-treatment-tags",
                             f"--record={tmp_path / 'imports.jsonl'}"], cwd=data_access,
                            env=os.environ | fake, text=True, capture_output=True)
    assert result.returncode != 0
    assert "records only imports it verified exist" in result.stderr
    assert (tmp_path / "imports.jsonl").read_text() == ""


def test_apply_recorded_runs_exactly_the_layers_recorded_entries(tmp_path):
    data_access = _layer(tmp_path, 'governance_mode = "deterministic"\n')
    fake = fake_uc.install(tmp_path, LIVE)
    record = tmp_path / "imports.jsonl"
    entries = [
        {"kind": "cleanup", "layer": "data_access", "entity_type": "tables", "entity_name": "cat.sch.tbl",
         "tag_key": "sensitivity", "statement": "ALTER TABLE cat.sch.tbl UNSET TAGS ('sensitivity')",
         "warehouse_id": "wh"},
        {"kind": "import", "layer": "data_access", "address": 'module.data_access.x.y["k"]', "id": "k-id"},
        {"kind": "import", "layer": "workspace", "address": 'module.workspace.x.y["w"]', "id": "w-id"},
    ]
    record.write_text("".join(json.dumps(e) + "\n" for e in entries))
    result = _run(data_access, fake, "--skip-treatment-tags", f"--apply-recorded={record}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert fake_uc.sql_writes(fake) == ["ALTER TABLE cat.sch.tbl UNSET TAGS ('sensitivity')"]
    assert [argv[3:] for argv in fake_uc.runner_imports(fake)] == [['module.data_access.x.y["k"]', "k-id"]]
    # A recorded import that fails fails the release (it was verified to exist).
    failing = _run(data_access, fake | {"FAKE_UC_FAIL_IMPORT": "1"}, "--skip-treatment-tags",
                   f"--apply-recorded={record}")
    assert failing.returncode == 1 and "recorded import(s) failed" in failing.stderr


def test_failed_optional_import_continues_and_surfaces_failure(tmp_path):
    data_access = tmp_path / "env" / "data_access"
    data_access.mkdir(parents=True)
    (tmp_path / "env" / "env.auto.tfvars").write_text('governance_mode = "legacy"\n')
    (data_access / "abac.auto.tfvars").write_text(
        'tag_assignments = [\n'
        '  { entity_type = "tables", entity_name = "cat.sch.first", tag_key = "k", tag_value = "v" },\n'
        '  { entity_type = "tables", entity_name = "cat.sch.second", tag_key = "k", tag_value = "v" }\n'
        ']\n'
    )
    runner = tmp_path / "terraform-runner"
    runner.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$3\" = state ]; then exit 0; fi\n"
        "echo \"$*\" >> \"$RUNNER_LOG\"\n"
        "if [[ \"$*\" == *first* ]]; then echo optional-import-failed; exit 1; fi\n"
    )
    runner.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    python = fake_bin / "python3"
    python.symlink_to(sys.executable)

    env = os.environ | {
        "TERRAFORM_RUNNER": str(runner),
        "RUNNER_LOG": str(tmp_path / "runner.log"),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    result = subprocess.run(
        ["bash", SCRIPT, "--tag-assignments-only"],
        cwd=data_access,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0
    assert "optional-import-failed" in result.stderr
    assert "Failed to import" in result.stdout
    log = (tmp_path / "runner.log").read_text()
    assert "first" in log
    assert "second" in log


@pytest.mark.parametrize(("manage_groups", "imports"), [("false", False), ("true", True)])
def test_account_group_import_runs_only_when_groups_are_managed(tmp_path, manage_groups, imports):
    """Consume mode has no databricks_group.groups resource to import into."""
    account = tmp_path / "account"
    account.mkdir()
    (account / "abac.auto.tfvars").write_text('groups = { tier_a = {} }\n')
    (account / "env.auto.tfvars").write_text(f"manage_groups = {manage_groups}\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # Stand-in for the group lookup: print one name:id pair.
    (fake_bin / "python3").write_text("#!/usr/bin/env bash\ncat >/dev/null; echo tier_a:123\n")
    (fake_bin / "python3").chmod(0o755)

    result = subprocess.run(
        ["bash", SCRIPT, "--groups-only", "--dry-run"],
        cwd=account,
        env=os.environ | {"PATH": f"{fake_bin}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert ('databricks_group.groups["tier_a"]' in result.stdout) is imports
