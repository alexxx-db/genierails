"""Deterministic rehearse/release only add tag-policy values; apply-account may remove.

Terraform ignores tag-policy ``values``, so an empty account plan doesn't make
the REST sync safe: these tests drive the real make targets with a fake
Databricks SDK (live values in a JSON file) and a Terraform runner that
reports an empty plan.
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
    "databricks/sdk/__init__.py": '''
import json, os
from types import SimpleNamespace
from databricks.sdk.service.tags import TagPolicy, Value

STATE = os.environ["FAKE_TAGS_STATE"]

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

class WorkspaceClient:
    def __init__(self, **kwargs):
        self.config = SimpleNamespace(authenticate=lambda: {})
        self.tag_policies = _Tags()
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
        gitconfig = tmp_path / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.com\n[commit]\n\tgpgsign = false\n"
                             "[init]\n\tdefaultBranch = main\n")
        self.env = {
            **{k: v for k, v in without_ci(os.environ).items() if k not in {"MAKEFLAGS", "MAKELEVEL", "TAG_SYNC_MODE"}},
            "PYTHONPATH": str(sdk), "PIP_NO_INDEX": "1",
            "FAKE_TAGS_STATE": str(self.state), "FAKE_TAGS_WRITES": str(self.writes),
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

    def account_manifest(self):
        result = self.make("account-plan", "account")
        assert result.returncode == 0, result.stdout + result.stderr
        return next(line.split(":", 1)[1].strip() for line in result.stdout.splitlines()
                    if "Manifest (immutable):" in line)

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


# (b) ... and refuses, naming the value and make apply-account, writing nothing.
def test_a_needed_removal_refuses_with_the_apply_account_pointer_and_writes_nothing(tmp_path):
    repo = Repo(tmp_path, LIVE)
    result = repo.make("_apply-account-for-workspace")
    assert result.returncode != 0
    out = result.stdout + result.stderr
    assert "gr_treatment: would remove ['retired_by_hand']" in out
    assert "make apply-account" in out
    assert repo.wrote() == []
    assert repo.live() == {k: sorted(v) for k, v in LIVE.items()}


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


def test_a_refusal_on_one_key_writes_no_other_key(tmp_path):
    live = {"gr_treatment": ["email_partial__string__0a1b2c3d"], "row_scope": ["anz", "emea"]}
    repo = Repo(tmp_path, live)
    (repo.account / "abac.auto.tfvars").write_text(DESIRED.replace(
        "]\n", '  { key = "row_scope", values = ["anz"] },\n]\n', 1))
    assert repo.script("--mode", "add-only").returncode == 1
    assert repo.wrote() == []


def test_standalone_sync_tags_for_a_deterministic_env_is_add_only(tmp_path):
    repo = Repo(tmp_path, LIVE)
    result = repo.make("sync-tags")
    assert result.returncode != 0 and "make apply-account" in result.stdout
    assert repo.wrote() == []


# (c) make apply-account (CI guard and account lock) may remove.
def test_apply_account_removes_values_no_longer_desired(tmp_path):
    repo = Repo(tmp_path, LIVE)
    result = repo.make("apply-account", "account", f"MANIFEST={repo.account_manifest()}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert repo.live()["gr_treatment"] == ["email_partial__string__0a1b2c3d", "new_value"]


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


def test_apply_account_refuses_if_live_replace_diff_changed_after_plan(tmp_path):
    repo = Repo(tmp_path, LIVE)
    manifest = repo.account_manifest()
    repo.state.write_text(json.dumps({"gr_treatment": ["email_partial__string__0a1b2c3d", "other_live"]}))
    result = repo.make("apply-account", "account", f"MANIFEST={manifest}")
    assert result.returncode != 0
    assert "tag-policy replace diff changed" in result.stderr
    assert repo.wrote() == []
    assert repo.live()["gr_treatment"] == ["email_partial__string__0a1b2c3d", "other_live"]


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
    assert repo.script().returncode == 1
    assert repo.wrote() == []
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
    assert result.returncode != 0 and "make apply-account" in result.stdout
    assert repo.wrote() == []


def test_standalone_sync_tags_replace_env_account_requires_live_account_lock(tmp_path):
    repo = Repo(tmp_path, LIVE)
    result = repo.make("sync-tags", "account", "TAG_SYNC_MODE=replace")
    assert result.returncode != 0
    assert "replace syncs may run only under the live account lock" in result.stderr
    assert repo.wrote() == []
    assert "retired_by_hand" in repo.live()["gr_treatment"]


def test_legacy_env_account_apply_is_gated_when_any_env_is_deterministic(tmp_path):
    repo = Repo(tmp_path, LIVE, mode="legacy")
    prod = repo.cloud / "envs" / "prod"
    (prod / "generated").mkdir(parents=True)
    (prod / "env.auto.tfvars").write_text('governance_mode = "deterministic"\n')
    result = repo.make("_apply-account-for-workspace", "dev")
    assert result.returncode != 0
    assert "make apply-account" in (result.stdout + result.stderr)
    assert repo.applies() == []
    assert repo.wrote() == []
    assert "retired_by_hand" in repo.live()["gr_treatment"]


def test_destroy_env_account_refuses_when_any_env_is_deterministic(tmp_path):
    repo = Repo(tmp_path, LIVE)
    result = repo.make("destroy", "account")
    assert result.returncode != 0
    assert "make destroy ENV=account refused" in result.stderr
    assert repo.applies() == []


# make -n runs recipe lines that call $(MAKE); the lock must not be one of them.
@pytest.mark.parametrize("args", [("apply-account", "dev"), ("apply", "account")])
def test_dry_run_takes_no_lock_and_writes_nothing(tmp_path, args):
    repo = Repo(tmp_path, LIVE)
    result = repo.make(args[0], args[1], "-n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not repo.lock.parent.exists() or list(repo.lock.parent.iterdir()) == []
    assert repo.applies() == [] and repo.wrote() == []
