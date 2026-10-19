"""Tag-policy value syncs: add-only never removes; removal is named, approved and proven unused.

Terraform ignores tag-policy ``values``, so an empty account plan doesn't make
the REST sync safe: these tests drive the real make targets with a fake
Databricks SDK (live values in a JSON file, tag assignments and ABAC policies
in another) and a Terraform runner that reports an empty plan.

On a shared metastore the live policy carries values other repos, teams or
legacy envs use. add-only leaves them in place (a warning, never a refusal);
only ``make account-plan ALLOW_TAG_VALUE_REMOVAL=...`` then ``make
apply-account`` removes a value, after checking it is unused.
"""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tests.script_stubs import without_ci

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "shared"
SCRIPT = SHARED / "scripts" / "sync_tag_policies.py"

FAKE_SDK = {
    "databricks/__init__.py": "",
    "databricks/sdk/useragent.py": "",
    "databricks/sdk/service/__init__.py": "",
    "databricks/sdk/service/tags.py": '''
class Value:
    def __init__(self, name):
        self.name = name

class TagPolicy:
    def __init__(self, tag_key, values=None):
        self.tag_key, self.values = tag_key, values or []
''',
    "databricks/sdk/service/sql.py": '''
import enum

class StatementState(enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
''',
    "databricks/sdk/__init__.py": '''
import json, os
from types import SimpleNamespace as NS
from databricks.sdk.service.sql import StatementState
from databricks.sdk.service.tags import TagPolicy, Value

STATE = os.environ["FAKE_TAGS_STATE"]

def _usage():
    return json.load(open(os.environ["FAKE_USAGE"]))

class _Tags:
    def list_tag_policies(self):
        live = json.load(open(STATE))
        return [TagPolicy(k, [Value(v) for v in vals]) for k, vals in live.items()]

    def update_tag_policy(self, tag_key, tag_policy, update_mask):
        live = json.load(open(STATE))
        live[tag_key] = [v.name for v in tag_policy.values]
        json.dump(live, open(STATE, "w"))
        with open(os.environ["FAKE_TAGS_WRITES"], "a") as log:
            log.write(tag_key + "\\n")

class _Sql:
    def execute_statement(self, statement, warehouse_id, wait_timeout=None):
        usage = _usage()
        with open(os.environ["FAKE_SQL_LOG"], "a") as log:
            log.write(json.dumps([warehouse_id, statement]) + "\\n")
        if usage.get("fail") == "sql":
            return NS(status=NS(state=StatementState.FAILED, error=NS(message="PERMISSION_DENIED", error_code="403")))
        if "DISTINCT" in statement:
            rows = [[t] for t in usage.get("keyed_tables", [])]
        else:
            rows = [r for r in usage.get("tag_rows", []) if "'" + r[3] + "'" in statement]
        return NS(status=NS(state=StatementState.SUCCEEDED), statement_id="s",
                  result=NS(data_array=rows, next_chunk_index=None), manifest=NS(total_chunk_count=1))

class _Catalogs:
    def list(self):
        return [NS(name=c) for c in _usage().get("catalogs", {"main": ["default"]})]

class _Schemas:
    def list(self, catalog_name):
        return [NS(name=s, full_name=catalog_name + "." + s)
                for s in _usage().get("catalogs", {"main": ["default"]})[catalog_name]]

class _Policies:
    def list_policies(self, on_securable_type, on_securable_fullname, include_inherited=None):
        usage = _usage()
        if usage.get("fail") == "policies":
            raise RuntimeError("PERMISSION_DENIED: list policies")
        return [NS(name=p["name"], when_condition=p.get("when_condition"),
                   match_columns=[NS(condition=c) for c in p.get("match_columns", [])])
                for p in usage.get("policies", {}).get(on_securable_type + ":" + on_securable_fullname, [])]

class WorkspaceClient:
    def __init__(self, **kwargs):
        self.config = NS(authenticate=lambda: {})
        self.tag_policies = _Tags()
        self.statement_execution = _Sql()
        self.catalogs = _Catalogs()
        self.schemas = _Schemas()
        self.policies = _Policies()
''',
}
# Terraform stand-in: every plan is empty and every apply succeeds; an apply
# records whether the account lock was held at that moment.
RUNNER = """#!/bin/sh
if [ "$3" = plan ]; then
  wrote_plan=0
  for arg in "$@"; do case "$arg" in -out=*) printf plan > "${arg#-out=}";; esac; done
  for arg in "$@"; do case "$arg" in -out=*) wrote_plan=1;; esac; done
  [ "$wrote_plan" = 1 ] && exit 2 || exit 0
fi
if [ "$3" = show ]; then
  printf '{"resource_changes":[{"change":{"actions":["update"]}}]}\\n'
  exit 0
fi
if [ "$3" = apply ] || [ "$3" = apply-plan ]; then
  if [ -f "$FAKE_LOCK_PATH" ]; then echo "apply locked" >> "$FAKE_RUNNER_LOG"; else echo "apply unlocked" >> "$FAKE_RUNNER_LOG"; fi
fi
exit 0
"""
DESIRED = '''tag_policies = [
  { key = "gr_treatment", description = "d", values = ["email_partial__string__0a1b2c3d", "new_value"] },
]
'''


class Repo:
    def __init__(self, tmp_path: Path, live: dict, *, mode: str = "deterministic"):
        self.tmp = tmp_path
        sdk = tmp_path / "sdk"
        for rel, text in FAKE_SDK.items():
            (sdk / rel).parent.mkdir(parents=True, exist_ok=True)
            (sdk / rel).write_text(text)
        self.root = tmp_path / "repo"
        self.cloud = self.root / "aws"
        self.account = self.cloud / "envs" / "account"
        self.dev = self.cloud / "envs" / "dev"
        for env_dir in (self.account, self.dev / "generated"):
            env_dir.mkdir(parents=True)
        (self.cloud / ".gitignore").write_text(".genierails-state/\nenvs/*/auth.auto.tfvars\nenvs/*/abac.auto.tfvars\nenvs/*/generated/.*\n")
        (self.account / "env.auto.tfvars").write_text("manage_groups = false\n")
        (self.account / "abac.auto.tfvars").write_text(DESIRED)
        (self.account / "auth.auto.tfvars").write_text(
            'databricks_account_id = "test-account"\n'
            'databricks_workspace_host = "https://unused.invalid"\n')
        (self.dev / "env.auto.tfvars").write_text(
            f'governance_mode = "{mode}"\n' if mode == "deterministic" else 'uc_tables = ["c.s.t"]\n')
        self.state_dir = tmp_path / "state-dir"
        self.state_dir.mkdir()
        self.runner = tmp_path / "runner.sh"
        self.runner.write_text(RUNNER)
        self.runner.chmod(0o755)
        self.state = tmp_path / "live.json"
        self.state.write_text(json.dumps(live))
        self.writes = tmp_path / "writes.log"
        self.writes.write_text("")
        self.usage = tmp_path / "usage.json"
        self.usage.write_text("{}")
        self.sql_log = tmp_path / "sql.log"
        gitconfig = tmp_path / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.com\n[commit]\n\tgpgsign = false\n"
                             "[init]\n\tdefaultBranch = main\n")
        self.env = {
            **{k: v for k, v in without_ci(os.environ).items() if k not in {"MAKEFLAGS", "MAKELEVEL", "TAG_SYNC_MODE"}},
            "PYTHONPATH": str(sdk), "PIP_NO_INDEX": "1",
            "FAKE_TAGS_STATE": str(self.state), "FAKE_TAGS_WRITES": str(self.writes),
            "FAKE_USAGE": str(self.usage), "FAKE_SQL_LOG": str(self.sql_log),
            "FAKE_LOCK_PATH": str(self.lock), "FAKE_RUNNER_LOG": str(tmp_path / "runner.log"),
            "GENIERAILS_STATE_DIR": str(self.state_dir), "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self._git("init", "-q", "-b", "main")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "init")
        self.origin = tmp_path / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True, env=self.env)
        self._git("remote", "add", "origin", str(self.origin))
        self._git("push", "-q", "origin", "main")

    def _git(self, *args):
        result = subprocess.run(["git", "-C", str(self.root), *args], env=self.env, text=True,
                                capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    @property
    def lock(self):
        return self.state_dir / "aws" / "account" / "locks" / ".governance.lock"

    def hold_lock(self):
        """The account lock, held by another live run (this test process)."""
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                                         "owner": "release", "started_at": "2026-10-11T00:00:00+00:00"}))

    def applies(self):
        log = self.tmp / "runner.log"
        return log.read_text().splitlines() if log.exists() else []

    def make(self, target, env="dev", *extra):
        return subprocess.run(
            ["make", "-f", str(ROOT / "aws" / "Makefile"), target, f"ENV={env}",
             f"CLOUD_ROOT={self.cloud}", f"SHARED_ROOT={SHARED}", f"ROOT_RUNNER={self.runner}", *extra],
            cwd=ROOT, env=self.env, text=True, capture_output=True, timeout=120,
        )

    def account_plan(self, *extra):
        return self.make("account-plan", "account", *extra)

    def account_manifest(self, *extra):
        result = self.account_plan(*extra)
        assert result.returncode == 0, result.stdout + result.stderr
        return next(line.split(":", 1)[1].strip() for line in result.stdout.splitlines()
                    if "Manifest (immutable):" in line)

    def use(self, **usage):
        self.usage.write_text(json.dumps(usage))

    def script(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.account, env=self.env,
                              text=True, capture_output=True, timeout=60)

    def live(self):
        return {k: sorted(v) for k, v in json.loads(self.state.read_text()).items()}

    def wrote(self):
        return self.writes.read_text().split()


LIVE = {"gr_treatment": ["email_partial__string__0a1b2c3d", "retired_by_hand"]}


# (a) The rehearse/release account step (deterministic): a live value that is
# no longer desired survives. On 80c0c4b this sync replaced the values and
# dropped it.
def test_rehearse_release_sync_keeps_a_live_value_that_is_not_desired(tmp_path):
    repo = Repo(tmp_path, LIVE)
    repo.make("_apply-account-for-workspace")
    assert "retired_by_hand" in repo.live()["gr_treatment"]


# (b) A shared metastore: every desired value is live, plus values other repos
# use. add-only has nothing to write, so it passes, warns and writes nothing.
# On d16bb51 it refused, failing make rehearse ENV=dev.
FOREIGN = ["aadhaar_partial", "redact", "ssn_last4"]
SHARED_LIVE = {"gr_treatment": ["email_partial__string__0a1b2c3d", "new_value", *FOREIGN]}


def test_add_only_with_foreign_values_and_nothing_missing_passes_and_writes_nothing(tmp_path):
    repo = Repo(tmp_path, SHARED_LIVE)
    result = repo.make("_apply-account-for-workspace")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"gr_treatment: 3 live value(s) not desired by this checkout are left in place: {FOREIGN}" in result.stdout
    assert repo.wrote() == []
    assert repo.live() == {k: sorted(v) for k, v in SHARED_LIVE.items()}


# The warning never steers to apply-account without saying it deletes values.
def test_the_left_in_place_warning_says_removal_deletes_for_every_workspace(tmp_path):
    repo = Repo(tmp_path, SHARED_LIVE)
    out = repo.make("_apply-account-for-workspace").stdout
    hint = out[out.index("This sync never removes values"):]
    assert "deletes it from the account tag policy for every workspace" in hint
    assert hint.index("deletes it") < hint.index("make apply-account")
    assert "ALLOW_TAG_VALUE_REMOVAL" in hint


def test_add_only_with_foreign_and_missing_values_adds_only_the_missing_ones(tmp_path):
    live = {"gr_treatment": ["email_partial__string__0a1b2c3d", *FOREIGN]}
    repo = Repo(tmp_path, live)
    result = repo.make("_apply-account-for-workspace")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "gr_treatment: added ['new_value']" in result.stdout and "removed" not in result.stdout
    assert repo.live()["gr_treatment"] == sorted(["email_partial__string__0a1b2c3d", "new_value", *FOREIGN])
    assert repo.wrote() == ["gr_treatment"]


def test_rehearse_release_sync_adds_missing_values(tmp_path):
    repo = Repo(tmp_path, {"gr_treatment": ["email_partial__string__0a1b2c3d"]})
    result = repo.make("_apply-account-for-workspace")
    assert result.returncode == 0, result.stdout + result.stderr
    assert repo.live()["gr_treatment"] == ["email_partial__string__0a1b2c3d", "new_value"]


def test_add_only_keeps_live_deterministic_generations_without_refusing(tmp_path):
    repo = Repo(tmp_path, {"gr_treatment": ["email_partial__string__0a1b2c3d", "email_partial__string__ffffffff"]})
    result = repo.make("_apply-account-for-workspace")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "email_partial__string__ffffffff" in repo.live()["gr_treatment"]


# --dry-run reports exactly what the real run does: exit code, warnings, refusals.
# On d16bb51 the dry run returned before the refusal check (exit 0, no output).
DRY_RUN_CASES = {
    "foreign-nothing-missing": (SHARED_LIVE, ()),
    "foreign-and-missing": ({"gr_treatment": ["email_partial__string__0a1b2c3d", *FOREIGN]}, ()),
    "refused-removal": (SHARED_LIVE, ("--remove", "gr_treatment:redact")),  # no warehouse: usage unknown
    "refused-desired-removal": (SHARED_LIVE, ("--remove", "gr_treatment:new_value", "--warehouse-id", "wh")),
}


def _outcome(result):
    lines = [line.replace("[PLAN]", "").replace("[SYNC]", "").replace("would add", "added")
             .replace("would remove", "removed").strip()
             for line in (result.stdout + result.stderr).splitlines()]
    return result.returncode, [line for line in lines if line.startswith(("[WARN]", "[ERROR]", "- gr_treatment"))]


@pytest.mark.parametrize("case", sorted(DRY_RUN_CASES))
def test_dry_run_and_real_run_report_the_same_outcome(tmp_path, case):
    live, extra = DRY_RUN_CASES[case]
    repo = Repo(tmp_path, live)
    dry = repo.script("--dry-run", *extra)
    assert repo.wrote() == []
    real = repo.script(*extra)
    assert _outcome(dry) == _outcome(real)
    assert (dry.returncode != 0) == case.startswith("refused")
    if case.startswith("refused"):
        assert "Tag-policy value removal refused; nothing was written" in dry.stderr
        assert repo.wrote() == []
    elif case == "foreign-and-missing":
        assert "[PLAN] gr_treatment: would add ['new_value']" in dry.stdout


def test_a_refusal_on_one_key_writes_no_other_key(tmp_path):
    live = {"gr_treatment": ["email_partial__string__0a1b2c3d", "new_value", "redact"], "row_scope": ["emea"]}
    repo = Repo(tmp_path, live)
    (repo.account / "abac.auto.tfvars").write_text(DESIRED.replace(
        "]\n", '  { key = "row_scope", values = ["anz", "emea"] },\n]\n', 1))
    repo.use(tag_rows=[["column", "c.s.t.ssn", "gr_treatment", "redact"]])
    result = repo.script("--remove", "gr_treatment:redact", "--warehouse-id", "wh")
    assert result.returncode == 1 and "still in use: column c.s.t.ssn" in result.stderr
    assert repo.wrote() == []


def test_standalone_sync_tags_for_a_deterministic_env_is_add_only(tmp_path):
    repo = Repo(tmp_path, LIVE)
    result = repo.make("sync-tags")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "left in place: ['retired_by_hand']" in result.stdout
    assert repo.live()["gr_treatment"] == ["email_partial__string__0a1b2c3d", "new_value", "retired_by_hand"]


# (c) make apply-account removes only what its approved manifest names.
def test_apply_account_without_an_opt_in_leaves_undesired_values_in_place(tmp_path):
    repo = Repo(tmp_path, LIVE)
    manifest = repo.account_manifest()
    result = repo.make("apply-account", "account", f"MANIFEST={manifest}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Tag values removed: none" in result.stdout
    assert repo.live()["gr_treatment"] == ["email_partial__string__0a1b2c3d", "new_value", "retired_by_hand"]


def test_account_plan_surfaces_removals_in_its_output_and_manifest(tmp_path):
    repo = Repo(tmp_path, LIVE)
    result = repo.account_plan("ALLOW_TAG_VALUE_REMOVAL=gr_treatment:retired_by_hand", "WAREHOUSE_ID=wh-1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "!! TAG VALUES THIS MANIFEST REMOVES (from every workspace on the metastore):" in result.stdout
    assert "!!   gr_treatment: ['retired_by_hand']" in result.stdout
    assert "Approving this manifest approves exactly the plan and the tag-value removals above." in result.stdout
    manifest = next(line.split(":", 1)[1].strip() for line in result.stdout.splitlines()
                    if "Manifest (immutable):" in line)
    data = json.loads(Path(manifest).read_text())
    assert data["tag_value_removal"] == {"allowed": ["gr_treatment:retired_by_hand"],
                                         "allowed_arg": "gr_treatment:retired_by_hand",
                                         "warehouse_id": "wh-1", "removed": ["gr_treatment:retired_by_hand"]}
    assert data["tag_sync"][0]["removed"] == ["retired_by_hand"]
    assert data["tag_sync"][0]["removal_check"]["warehouse_id"] == "wh-1"
    assert repo.wrote() == []
    # The usage check reads every information_schema tag view through the named warehouse.
    sql = [json.loads(line) for line in repo.sql_log.read_text().splitlines()]
    assert {wh for wh, _ in sql} == {"wh-1"}
    for view in ("catalog_tags", "schema_tags", "table_tags", "column_tags", "volume_tags"):
        assert f"system.information_schema.{view}" in sql[0][1]


def test_an_opt_in_removal_of_an_unused_value_works(tmp_path):
    repo = Repo(tmp_path, LIVE)
    manifest = repo.account_manifest("ALLOW_TAG_VALUE_REMOVAL=gr_treatment:retired_by_hand", "WAREHOUSE_ID=wh")
    result = repo.make("apply-account", "account", f"MANIFEST={manifest}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "gr_treatment: added ['new_value'], removed ['retired_by_hand']" in result.stdout
    assert repo.live()["gr_treatment"] == ["email_partial__string__0a1b2c3d", "new_value"]


@pytest.mark.parametrize("usage, why", [
    ({"tag_rows": [["column", "other.s.t.ssn", "gr_treatment", "retired_by_hand"]]},
     "still in use: column other.s.t.ssn"),
    ({"tag_rows": [["schema", "other.s", "GR_TREATMENT", "retired_by_hand"]]}, "still in use: schema other.s"),
    ({"catalogs": {"other": ["s"]}, "policies": {"CATALOG:other": [{
        "name": "mask_ssn", "match_columns": ["hasTagValue('gr_treatment', 'retired_by_hand')"]}]}},
     "still in use: ABAC policy mask_ssn on catalog other"),
    ({"catalogs": {"other": ["s"]}, "keyed_tables": ["other.s.t"], "policies": {"TABLE:other.s.t": [{
        "name": "row_f", "when_condition": "hasTagValue('gr_treatment','retired_by_hand')"}]}},
     "still in use: ABAC policy row_f on table other.s.t"),
], ids=["column-tag", "schema-tag-any-case", "catalog-policy", "table-policy"])
def test_an_in_use_value_is_refused_and_no_manifest_is_written(tmp_path, usage, why):
    repo = Repo(tmp_path, LIVE)
    repo.use(**usage)
    result = repo.account_plan("ALLOW_TAG_VALUE_REMOVAL=gr_treatment:retired_by_hand", "WAREHOUSE_ID=wh")
    assert result.returncode != 0
    assert f"gr_treatment:retired_by_hand: {why}" in result.stderr
    assert "Manifest (immutable)" not in result.stdout
    assert repo.wrote() == []


@pytest.mark.parametrize("usage, extra, why", [
    ({}, (), "ALLOW_TAG_VALUE_REMOVAL needs WAREHOUSE_ID"),
    ({"fail": "sql"}, ("WAREHOUSE_ID=wh",), "usage unknown: could not read tag assignments"),
    ({"fail": "policies"}, ("WAREHOUSE_ID=wh",), "usage unknown: could not list ABAC policies on catalog main"),
], ids=["no-warehouse", "sql-fails", "policy-listing-fails"])
def test_an_unknown_usage_value_is_refused(tmp_path, usage, extra, why):
    repo = Repo(tmp_path, LIVE)
    repo.use(**usage)
    result = repo.account_plan("ALLOW_TAG_VALUE_REMOVAL=gr_treatment:retired_by_hand", *extra)
    assert result.returncode != 0 and why in result.stderr
    assert "Manifest (immutable)" not in result.stdout
    assert repo.wrote() == []


@pytest.mark.parametrize("value, why", [
    ("new_value", "still desired by this checkout"),
    ("email_partial__string__ffffffff", "a deterministic generation"),
    ("never_live", "not a live value of the tag policy"),
])
def test_an_opt_in_removal_the_sync_must_not_make_is_refused(tmp_path, value, why):
    repo = Repo(tmp_path, {"gr_treatment": LIVE["gr_treatment"] + ["email_partial__string__ffffffff"]})
    result = repo.account_plan(f"ALLOW_TAG_VALUE_REMOVAL=gr_treatment:{value}", "WAREHOUSE_ID=wh")
    assert result.returncode != 0 and f"gr_treatment:{value}: {why}" in result.stderr
    assert repo.wrote() == []


def test_apply_account_refuses_if_untracked_account_input_changed_after_plan(tmp_path):
    live = {"gr_treatment": ["email_partial__string__0a1b2c3d", "new_value", "keep_me"]}
    repo = Repo(tmp_path, live)
    (repo.account / "abac.auto.tfvars").write_text(DESIRED.replace('"new_value"', '"new_value", "keep_me"'))
    manifest = repo.account_manifest()
    (repo.account / "abac.auto.tfvars").write_text(DESIRED)
    result = repo.make("apply-account", "account", f"MANIFEST={manifest}")
    assert result.returncode != 0
    assert "account input file hashes changed" in result.stderr
    assert repo.wrote() == []
    assert repo.live()["gr_treatment"] == ["email_partial__string__0a1b2c3d", "keep_me", "new_value"]


def test_apply_account_refuses_if_live_removals_drifted_from_the_plan(tmp_path):
    live = {"gr_treatment": ["email_partial__string__0a1b2c3d", "new_value", "retired_by_hand", "old_b"]}
    repo = Repo(tmp_path, live)
    manifest = repo.account_manifest("ALLOW_TAG_VALUE_REMOVAL=gr_treatment:retired_by_hand,gr_treatment:old_b",
                                     "WAREHOUSE_ID=wh")
    # Someone else removed one planned value: the live removal set is now smaller.
    repo.state.write_text(json.dumps({"gr_treatment": ["email_partial__string__0a1b2c3d", "new_value", "old_b"]}))
    result = repo.make("apply-account", "account", f"MANIFEST={manifest}")
    assert result.returncode != 0
    assert "gr_treatment:retired_by_hand: not a live value" in result.stderr
    assert repo.wrote() == []
    assert repo.live()["gr_treatment"] == ["email_partial__string__0a1b2c3d", "new_value", "old_b"]


def test_apply_account_refuses_if_planned_additions_drifted(tmp_path):
    repo = Repo(tmp_path, LIVE)
    manifest = repo.account_manifest()
    repo.state.write_text(json.dumps({"gr_treatment": ["email_partial__string__0a1b2c3d", "new_value"]}))
    result = repo.make("apply-account", "account", f"MANIFEST={manifest}")
    assert result.returncode != 0
    assert "the account tag-policy diff changed since account-plan: planned removals [], additions ['new_value']; " \
           "live removals now [], additions []" in result.stderr
    assert repo.wrote() == []


def test_apply_account_ignores_foreign_values_appearing_after_plan(tmp_path):
    repo = Repo(tmp_path, LIVE)
    manifest = repo.account_manifest()
    repo.state.write_text(json.dumps({"gr_treatment": LIVE["gr_treatment"] + ["another_teams_value"]}))
    result = repo.make("apply-account", "account", f"MANIFEST={manifest}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "another_teams_value" in repo.live()["gr_treatment"]


def test_apply_account_refuses_a_value_that_came_into_use_after_plan(tmp_path):
    repo = Repo(tmp_path, LIVE)
    manifest = repo.account_manifest("ALLOW_TAG_VALUE_REMOVAL=gr_treatment:retired_by_hand", "WAREHOUSE_ID=wh")
    repo.use(tag_rows=[["column", "other.s.t.ssn", "gr_treatment", "retired_by_hand"]])
    result = repo.make("apply-account", "account", f"MANIFEST={manifest}")
    assert result.returncode != 0 and "still in use: column other.s.t.ssn" in result.stderr
    assert repo.wrote() == [] and "retired_by_hand" in repo.live()["gr_treatment"]


def test_apply_account_refuses_an_allow_list_that_differs_from_the_manifest(tmp_path):
    repo = Repo(tmp_path, {"gr_treatment": LIVE["gr_treatment"] + ["other_old"]})
    manifest = repo.account_manifest("ALLOW_TAG_VALUE_REMOVAL=gr_treatment:retired_by_hand", "WAREHOUSE_ID=wh")
    result = repo.make("apply-account", "account", f"MANIFEST={manifest}",
                       "ALLOW_TAG_VALUE_REMOVAL=gr_treatment:retired_by_hand,gr_treatment:other_old")
    assert result.returncode != 0
    assert "removals are chosen at account-plan, not at apply-account" in result.stderr
    assert repo.wrote() == []


def test_apply_account_keeps_the_ci_guard(tmp_path):
    repo = Repo(tmp_path, LIVE)
    repo.env["CI"] = "true"
    result = repo.make("apply-account")
    assert result.returncode != 0 and "cannot run in CI" in result.stderr
    assert repo.wrote() == []


# (d) Legacy envs are unchanged: their account apply still replaces (it was
# never add-only), through _apply-account-for-workspace and sync-tags alike.
@pytest.mark.parametrize("target", ["_apply-account-for-workspace", "sync-tags"])
def test_legacy_sync_still_replaces(tmp_path, target):
    repo = Repo(tmp_path, LIVE, mode="legacy")
    result = repo.make(target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert repo.live()["gr_treatment"] == ["email_partial__string__0a1b2c3d", "new_value"]


def test_the_script_defaults_to_add_only(tmp_path):
    repo = Repo(tmp_path, LIVE)
    assert repo.script().returncode == 0
    assert "retired_by_hand" in repo.live()["gr_treatment"]
    assert repo.script("--mode", "replace").returncode == 0
    assert "retired_by_hand" not in repo.live()["gr_treatment"]


# apply-account holds the account lock for the whole account apply.
def test_apply_account_refuses_while_the_account_lock_is_held_and_writes_nothing(tmp_path):
    repo = Repo(tmp_path, LIVE)
    manifest = repo.account_manifest()
    repo.hold_lock()
    result = repo.make("apply-account", "account", f"MANIFEST={manifest}")
    assert result.returncode != 0
    assert "is locked" in result.stderr and "held by make release" in result.stderr
    assert repo.applies() == [] and repo.wrote() == []
    assert repo.live() == {k: sorted(v) for k, v in LIVE.items()}


def test_apply_account_applies_under_the_lock_and_releases_it(tmp_path):
    repo = Repo(tmp_path, LIVE)
    result = repo.make("apply-account", "account", f"MANIFEST={repo.account_manifest()}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert repo.applies() == ["apply locked"]
    assert not repo.lock.exists()


# make apply ENV=account, while any env of this checkout is deterministic,
# goes through apply-account: same manifest gate before any account lock or removals.
def test_apply_env_account_with_a_deterministic_env_requires_an_account_manifest_before_locking(tmp_path):
    repo = Repo(tmp_path, LIVE)
    repo.hold_lock()
    result = repo.make("apply", "account")
    assert result.returncode != 0 and "make account-plan" in result.stderr
    assert repo.applies() == [] and repo.wrote() == []
    assert "retired_by_hand" in repo.live()["gr_treatment"]


def test_apply_env_account_with_a_deterministic_env_does_not_bypass_the_manifest_gate(tmp_path):
    repo = Repo(tmp_path, LIVE)
    result = repo.make("apply", "account")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "through make apply-account" in result.stdout
    assert "make account-plan" in result.stderr
    assert repo.applies() == []
    assert "retired_by_hand" in repo.live()["gr_treatment"]
    assert not repo.lock.exists()


def test_legacy_apply_env_account_is_unchanged(tmp_path):
    repo = Repo(tmp_path, LIVE, mode="legacy")
    result = repo.make("apply", "account")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "through make apply-account" not in result.stdout
    assert repo.applies() == ["apply unlocked"]
    assert repo.live()["gr_treatment"] == ["email_partial__string__0a1b2c3d", "new_value"]


def test_standalone_sync_tags_env_account_with_a_deterministic_env_is_add_only(tmp_path):
    repo = Repo(tmp_path, LIVE)
    result = repo.make("sync-tags", "account")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "retired_by_hand" in repo.live()["gr_treatment"]


def test_standalone_sync_tags_replace_with_a_deterministic_env_is_refused(tmp_path):
    repo = Repo(tmp_path, LIVE)
    result = repo.make("sync-tags", "account", "TAG_SYNC_MODE=replace")
    assert result.returncode != 0
    assert "replace would delete every live tag-policy value this checkout doesn't desire" in result.stderr
    assert repo.wrote() == []
    assert "retired_by_hand" in repo.live()["gr_treatment"]


def test_standalone_sync_tags_removal_requires_the_live_account_lock(tmp_path):
    repo = Repo(tmp_path, LIVE)
    result = repo.make("sync-tags", "account", "TAG_SYNC_REMOVE=gr_treatment:retired_by_hand", "WAREHOUSE_ID=wh")
    assert result.returncode != 0
    assert "tag-value removals may run only under the live account lock" in result.stderr
    assert repo.wrote() == []
    assert "retired_by_hand" in repo.live()["gr_treatment"]


def test_legacy_env_account_apply_is_gated_when_any_env_is_deterministic(tmp_path):
    repo = Repo(tmp_path, LIVE, mode="legacy")
    prod = repo.cloud / "envs" / "prod"
    (prod / "generated").mkdir(parents=True)
    (prod / "env.auto.tfvars").write_text('governance_mode = "deterministic"\n')
    result = repo.make("_apply-account-for-workspace", "dev")
    # add-only, never the legacy replace: no account apply, nothing removed.
    assert result.returncode == 0, result.stdout + result.stderr
    assert "left in place: ['retired_by_hand']" in result.stdout
    assert repo.applies() == []
    assert repo.live()["gr_treatment"] == ["email_partial__string__0a1b2c3d", "new_value", "retired_by_hand"]


def test_destroy_env_account_refuses_when_any_env_is_deterministic(tmp_path):
    repo = Repo(tmp_path, LIVE)
    result = repo.make("destroy", "account")
    assert result.returncode != 0
    assert "make destroy ENV=account refused" in result.stderr
    assert repo.applies() == []


@pytest.mark.parametrize("target", ["destroy", "_destroy-layer", "sync-tags"])
def test_account_mutation_guards_fail_closed_on_malformed_workspace_config(tmp_path, target):
    live = {"gr_treatment": ["email_partial__string__0a1b2c3d", "retired_by_hand"]}
    repo = Repo(tmp_path, live)
    (repo.dev / "env.auto.tfvars").write_text('governance_mode = "deterministic" ]]\n')
    if target == "destroy":
        result = repo.make("destroy", "account")
    elif target == "_destroy-layer":
        result = repo.make("_destroy-layer", "account", "LAYER=account", "TARGET_ENV=account",
                           f"LAYER_ENV_DIR={repo.account}")
    else:
        result = repo.make("sync-tags", "account", "TAG_SYNC_MODE=replace")
    assert result.returncode != 0
    assert "cannot determine whether deterministic governance is in use" in result.stderr
    assert repo.applies() == [] and repo.wrote() == []
    assert "retired_by_hand" in repo.live()["gr_treatment"]


# make -n runs recipe lines that call $(MAKE); the lock must not be one of them.
@pytest.mark.parametrize("args", [("apply-account", "dev"), ("apply", "account")])
def test_dry_run_takes_no_lock_and_writes_nothing(tmp_path, args):
    repo = Repo(tmp_path, LIVE)
    result = repo.make(args[0], args[1], "-n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not repo.lock.parent.exists() or list(repo.lock.parent.iterdir()) == []
    assert repo.applies() == [] and repo.wrote() == []
