"""make wiring of release gating (rollout step 9): rehearse, release-plan, release MANIFEST=, maintain.

Real make runs the real recipes and release_gate.py against a git checkout
in tmp_path (tests/test_release_gate.py's Repo). Recursive makes, Terraform
and every live check are stubs that record their calls, so nothing reaches
Databricks.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import release_gate as rg
from tests import script_stubs
from tests.test_release_gate import EMPTY_ABAC, PROD_ENV, Repo

ROOT = Path(__file__).resolve().parents[2]
LEGACY_ENV = 'promote_from = "dev"\n'

MAKE_STUB = r'''#!{python}
"""Recursive make: records its arguments; _release-plan-roots saves fake plans."""
import json, hashlib, os, sys
from pathlib import Path
args = sys.argv[1:]
with open({log!r}, "a") as log:
    log.write(json.dumps(args) + "\n")
if {fail!r} and {fail!r} in args:
    sys.exit(1)
if set(args) & set({passthrough!r}):
    # Run the real recipe (its own recursive makes come back to this stub via MAKEFLAGS).
    os.execvp("make", ["make", *args])
if "_release-plan-roots" in args:
    plan_dir = Path(next(a.split("=", 1)[1] for a in args if a.startswith("PLAN_DIR=")))
    (plan_dir / "account.tfplan").write_bytes(b"account")
    (plan_dir / "account.json").write_text('{{"file": "account.tfplan", "changes": false}}')
    (plan_dir / "workspace.tfplan").write_bytes(b"workspace")
    (plan_dir / "workspace.json").write_text('{{"file": "workspace.tfplan", "changes": true}}')
    if Path({imports!r}).exists():  # imports and stale-tag cleanups import_existing.sh --record found
        (plan_dir / "imports.jsonl").write_text(Path({imports!r}).read_text())
    da = plan_dir / "data_access"
    da.mkdir()
    (da / "00-grant.tfplan").write_bytes(b"grant")
    (da / "phases.json").write_text(json.dumps({{
        "transition": {{"from": "a", "to": "a", "retain": "", "retire": "", "tier_move": False,
                       "phases": ["create", "grant"], "completed": [], "resumed": False}},
        "steps": [{{"phase": "grant", "kind": "apply", "file": "00-grant.tfplan",
                   "sha256": hashlib.sha256(b"grant").hexdigest(), "changes": True}}],
        "final": True}}))
'''

RECORDER = r'''#!{python}
import json, sys
from pathlib import Path
with open({log!r}, "a") as log:
    log.write(json.dumps([{name!r}] + sys.argv[1:]) + "\n")
args = sys.argv[1:]
if {name!r} == "reports" and "--plan-dir" in args:
    Path(args[args.index("--plan-dir") + 1], "reports.json").write_text(json.dumps({{"ok": True}}))
if {name!r} == "live":
    print(Path({live!r}).read_text().strip())
sys.exit(int(Path({rc!r}).read_text()) if Path({rc!r}).exists() else 0)
'''


class Make:
    def __init__(self, tmp_path: Path, repo: Repo, cloud: str = "aws", fail: str = "", passthrough=()):
        self.tmp, self.repo, self.cloud = tmp_path, repo, cloud
        self.log = tmp_path / "calls.jsonl"
        self.live = tmp_path / "live.txt"
        self.live.write_text("live-1\n")
        self.imports = tmp_path / "imports.jsonl"
        self.make = self._write("make-stub", MAKE_STUB.format(python=sys.executable, log=str(self.log), fail=fail,
                                                              passthrough=list(passthrough),
                                                              imports=str(self.imports)))
        self.stubs = {name: self._recorder(name) for name in
                      ("staged", "gate", "runner", "reports", "live", "inventory", "classification", "audit")}

    def _write(self, name: str, text: str) -> Path:
        path = self.tmp / name
        path.write_text(text)
        path.chmod(0o755)
        return path

    def _recorder(self, name: str) -> Path:
        return self._write(f"{name}-stub", RECORDER.format(python=sys.executable, log=str(self.log), name=name,
                                                           live=str(self.live), rc=str(self.tmp / f"{name}.rc")))

    def exit_code(self, name: str, rc: int) -> None:
        (self.tmp / f"{name}.rc").write_text(str(rc))

    def script_stubs(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """The live checks, stubbed below make (the helpers themselves can't be overridden)."""
        return {
            "table_readers.py check": "true", "group_map.py": "true", "deployer_identity.py": "true",
            "saved_settings.py verify-key": "true", "staged_policy_apply.py confirm": "true",
            "external_mask_inventory.py": str(self.stubs["inventory"]),
            "classification_completeness.py check": str(self.stubs["classification"]),
            "release_gate.py live-snapshot": str(self.stubs["live"]),
            "release_gate.py reports": str(self.stubs["reports"]),
            "staged_policy_apply.py": str(self.stubs["staged"]), "coverage_gate.py": str(self.stubs["gate"]),
            "audit_schema_drift.py": str(self.stubs["audit"]), **(extra or {}),
        }

    def __call__(self, target: str, env: str, *extra: str, stub_make: bool = True, ci: bool = False,
                 stubs: dict[str, str] | None = None, environ_extra: dict[str, str] | None = None):
        env_dir = self.repo.cloud / "envs" / env
        args = ["make", "--no-print-directory", target, f"ENV={env}", f"ENV_DIR={env_dir}",
                f"ACCOUNT_ENV_DIR={self.repo.cloud / 'envs' / 'account'}",
                f"ROOT_RUNNER={self.stubs['runner']}", *extra]
        if stub_make:
            args.append(f"MAKE={self.make}")
        bin_dir = script_stubs.install(self.tmp / "stub-bin", self.script_stubs(stubs))
        environ = {k: v for k, v in os.environ.items()
                   if k not in ("MAKEFLAGS", "MAKELEVEL", "CI", "VERIFY_KEY_COLUMN", "APPLY_FLAGS", "MANIFEST")}
        environ |= self.repo.env | ({"CI": "true"} if ci else {}) | (environ_extra or {})
        environ["PATH"] = f"{bin_dir}:{environ['PATH']}"
        return subprocess.run(args, cwd=ROOT / self.cloud, text=True, capture_output=True, env=environ)

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        # Drop the scratch promote-to the receipt digest runs (make -C <scratch> promote-to).
        return [c for c in map(json.loads, self.log.read_text().splitlines()) if c[:1] != ["-C"]]

    def names(self) -> list[str]:
        return [next((a for a in c if not a.startswith("-") and "=" not in a), "?") for c in self.calls()]

    def reset(self) -> None:
        self.log.unlink(missing_ok=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    return Repo(tmp_path, monkeypatch)


@pytest.fixture
def make(tmp_path, repo):
    return Make(tmp_path, repo)


def _no_lock(repo: Repo, env: str) -> bool:
    return not (repo.cloud / "envs" / env / "generated" / ".governance.lock").exists()


# -- plain release in a deterministic env ---------------------------------------------

@pytest.mark.parametrize("cloud", ["aws", "azure"])
def test_plain_release_in_a_deterministic_env_refuses_with_a_pointer_to_release_plan(tmp_path, repo, cloud):
    result = Make(tmp_path, repo, cloud)("release", "prod", stub_make=False)
    assert result.returncode != 0
    assert "make release-plan ENV=prod" in result.stderr and "MANIFEST=" in result.stderr
    assert _no_lock(repo, "prod")
    assert not rg.release_receipts(repo.cloud, "prod")


def test_release_hands_a_deterministic_env_to_the_manifest_release(make, repo):
    result = make("release", "prod", "MANIFEST=/m/manifest.json")
    assert result.returncode == 0, result.stderr
    assert make.calls() == [["--no-print-directory", "_release-manifest", "ENV=prod",
                             f"ENV_DIR={repo.prod}", "MANIFEST=/m/manifest.json"]]


def test_legacy_release_is_unchanged(make, repo):
    (repo.prod / "env.auto.tfvars").write_text(LEGACY_ENV)
    result = make("release", "prod")
    assert result.returncode == 0, result.stdout + result.stderr
    names = make.names()
    assert "_release-manifest" not in names
    # The classification check is stubbed below make now, so it is recorded too.
    assert names[:5] == ["classification", "_verify-access-keys-readonly", "derive-assignments",
                         "validate-generated", "coverage-gate"]
    assert not rg.release_receipts(repo.cloud, "prod")


# -- release-plan -----------------------------------------------------------------------

def test_release_plan_refuses_a_legacy_env(make, repo):
    (repo.prod / "env.auto.tfvars").write_text(LEGACY_ENV)
    result = make("release-plan", "prod")
    assert result.returncode != 0 and "not in deterministic governance mode" in result.stderr
    assert make.calls() == []


def test_release_plan_refuses_an_unrehearsed_commit_before_locking_or_running_anything(make, repo):
    result = make("release-plan", "prod")
    assert result.returncode != 0
    assert "no passing rehearsal of" in result.stderr
    assert make.calls() == [] and _no_lock(repo, "prod")


def test_release_plan_refuses_a_dirty_checkout(make, repo):
    assert make("rehearse", "dev").returncode == 0
    (repo.prod / "env.auto.tfvars").write_text(PROD_ENV + "# local edit\n")
    make.reset()
    result = make("release-plan", "prod")
    assert result.returncode != 0 and "checkout is not clean" in result.stderr and make.calls() == []


def test_release_plan_writes_one_manifest_under_the_lock(make, repo):
    assert make("rehearse", "dev").returncode == 0
    make.reset()
    result = make("release-plan", "prod")
    assert result.returncode == 0, result.stdout + result.stderr
    names = make.names()
    # The live checks run before the lock; everything after them runs under it.
    assert names == ["inventory", "classification", "derive-assignments", "validate-generated", "coverage-gate",
                     "promote", "verify-access-keys", "audit-rulebook", "reports", "_release-plan-roots",
                     "live"], names
    manifest = Path(result.stdout.split("make release ENV=prod MANIFEST=")[-1].strip())
    data = json.loads(manifest.read_text())
    assert data["commit"] == repo.head() and data["live_snapshot"] == "live-1" and data["final"] is True
    assert data["provenance"]["source_env"] == "dev" and data["reports"] == {"ok": True}
    assert _no_lock(repo, "prod")


def test_release_plan_refused_by_the_reports_writes_no_manifest(make, repo):
    assert make("rehearse", "dev").returncode == 0
    make.exit_code("reports", 1)
    result = make("release-plan", "prod")
    assert result.returncode != 0 and "reader or weakening check" in result.stderr
    assert not list(rg.manifests_dir(repo.cloud, "prod").glob("*/manifest.json"))
    assert not list(rg.manifests_dir(repo.cloud, "prod").glob(".pending-*"))


@pytest.mark.parametrize("cloud", ["aws", "azure"])
def test_release_plan_refuses_in_ci(tmp_path, repo, cloud):
    result = Make(tmp_path, repo, cloud)("release-plan", "prod", ci=True)
    assert result.returncode != 0 and "applying targets cannot run in CI" in result.stderr


# -- release MANIFEST= ---------------------------------------------------------------

def _planned(make: Make) -> Path:
    assert make("rehearse", "dev").returncode == 0
    result = make("release-plan", "prod")
    assert result.returncode == 0, result.stdout + result.stderr
    make.reset()
    return Path(result.stdout.split("make release ENV=prod MANIFEST=")[-1].strip())


def test_release_applies_only_the_saved_plans_then_verifies_and_writes_a_receipt(make, repo):
    manifest = _planned(make)
    result = make("_release-manifest", "prod", f"MANIFEST={manifest}")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = make.calls()
    staged = [c for c in calls if c[0] == "staged"]
    assert staged and staged[0][1:2] == ["apply-saved"]
    assert staged[0][staged[0].index("--plan-dir") + 1] == str(manifest.parent / "data_access")
    runner = [c for c in calls if c[0] == "runner"]
    assert runner == [["runner", "workspace", "prod", "apply-plan", str(manifest.parent / "workspace.tfplan")]]
    assert not [c for c in calls if c[0] == "runner" and ("plan" in c or "apply" in c)]
    assert "verify-access" in make.names()
    receipt = rg.last_release(repo.cloud, "prod")
    assert receipt["commit"] == repo.head() and receipt["manifest"]["path"] == str(manifest)


@pytest.mark.parametrize("drift", ["commit", "serial", "live", "plan"])
def test_release_refuses_a_drifted_manifest_and_applies_nothing(make, repo, drift):
    manifest = _planned(make)
    if drift == "commit":
        repo.commit({"aws/envs/prod/ack.txt": "# later\n"})
    elif drift == "serial":
        state = rg.root_state_paths(repo.prod)["workspace"]
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"version": 4, "serial": 3, "lineage": "L"}))
    elif drift == "live":
        make.live.write_text("live-2\n")
    else:
        plan = manifest.parent / "workspace.tfplan"
        os.chmod(plan, 0o644)
        plan.write_bytes(b"other")
    result = make("_release-manifest", "prod", f"MANIFEST={manifest}")
    assert result.returncode != 0
    assert "Run make release-plan ENV=prod again" in result.stderr
    assert not [c for c in make.calls() if c[0] in ("staged", "runner")]
    assert not rg.release_receipts(repo.cloud, "prod") and _no_lock(repo, "prod")


def test_a_phase_manifest_records_the_phase_and_writes_no_release_receipt(make, repo):
    manifest = _planned(make)
    make.exit_code("staged", 10)
    result = make("_release-manifest", "prod", f"MANIFEST={manifest}")
    assert result.returncode == 0, result.stderr
    assert "make release-plan ENV=prod" in result.stdout
    assert not rg.release_receipts(repo.cloud, "prod")
    assert [r["id"] for r in rg.phase_records(repo.cloud, "prod", repo.head())]
    assert not [c for c in make.calls() if c[0] == "runner"]  # the workspace waits for the last phase


def test_an_import_manifest_runs_only_its_recorded_imports_under_the_lock(make, repo):
    """release-plan recorded imports instead of running them; release runs exactly those, then stops."""
    entry = {"kind": "import", "layer": "data_access", "address": 'module.data_access.x.y["k"]', "id": "k"}
    make.imports.write_text(json.dumps(entry) + "\n")
    manifest = _planned(make)
    assert json.loads(manifest.read_text())["imports"]["count"] == 1
    importer = make._recorder("importer")
    (repo.prod / "data_access").mkdir(exist_ok=True)
    result = make("_release-manifest", "prod", f"MANIFEST={manifest}", f"IMPORT_EXISTING_SCRIPT={importer}")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = make.calls()
    assert [c for c in calls if c[0] == "importer"] == [
        ["importer", "--skip-treatment-tags", f"--apply-recorded={manifest.parent / 'imports.jsonl'}"]]
    # Nothing else: no saved plan, no tag-policy sync, no workspace apply, no release receipt.
    assert not [c for c in calls if c[0] in ("staged", "runner")] and "sync-tags" not in make.names()
    assert [r["id"] for r in rg.phase_records(repo.cloud, "prod", repo.head())]
    assert not rg.release_receipts(repo.cloud, "prod") and _no_lock(repo, "prod")
    assert "make release-plan ENV=prod" in result.stdout


def test_an_import_manifest_whose_record_changed_is_refused(make, repo):
    make.imports.write_text(json.dumps({"kind": "import", "layer": "data_access", "address": "a", "id": "b"}) + "\n")
    manifest = _planned(make)
    record = manifest.parent / "imports.jsonl"
    os.chmod(record, 0o644)
    record.write_text(json.dumps({"kind": "import", "layer": "data_access", "address": "a", "id": "other"}) + "\n")
    importer = make._recorder("importer")
    result = make("_release-manifest", "prod", f"MANIFEST={manifest}", f"IMPORT_EXISTING_SCRIPT={importer}")
    assert result.returncode != 0 and "the recorded imports" in result.stderr
    assert not [c for c in make.calls() if c[0] in ("importer", "staged", "runner")]


def test_a_failed_saved_plan_writes_no_release_receipt(make, repo):
    manifest = _planned(make)
    make.exit_code("staged", 1)
    result = make("_release-manifest", "prod", f"MANIFEST={manifest}")
    assert result.returncode != 0 and not rg.release_receipts(repo.cloud, "prod")


# -- maintain --------------------------------------------------------------------------

def test_maintain_refuses_before_any_release(make, repo):
    result = make("maintain", "prod")
    assert result.returncode != 0 and "no release receipt yet" in result.stderr
    assert make.calls() == [] and _no_lock(repo, "prod")


def test_maintain_runs_only_from_the_released_commit(make, repo):
    manifest = _planned(make)
    assert make("_release-manifest", "prod", f"MANIFEST={manifest}").returncode == 0
    make.reset()
    result = make("maintain", "prod")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "apply-governance" in make.names()
    repo.commit({"aws/envs/prod/ack.txt": "# newer, unreleased\n"})
    make.reset()
    result = make("maintain", "prod")
    assert result.returncode != 0 and "but the last release is" in result.stderr and make.calls() == []


def test_legacy_maintain_is_unchanged(make, repo):
    (repo.prod / "env.auto.tfvars").write_text(LEGACY_ENV)
    result = make("maintain", "prod")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "apply-governance" in make.names()


# -- rehearse --------------------------------------------------------------------------

def test_rehearse_writes_a_receipt_and_reuses_it_without_reapplying(make, repo):
    result = make("rehearse", "dev")
    assert result.returncode == 0, result.stdout + result.stderr
    assert make.names() == ["_derive-before-exposure", "validate-generated", "coverage-gate", "apply",
                            "verify-access"]
    first = rg.passing_receipt(repo.cloud, "dev", repo.head())
    assert first and first["applied"] is True and first["targets"]["prod"]["digest"]
    commit = repo.commit({"aws/envs/prod/ack.txt": "# prod-only change\n"})
    make.reset()
    result = make("rehearse", "dev")
    assert result.returncode == 0, result.stdout + result.stderr
    assert make.calls() == [] and "without re-applying" in result.stdout
    assert rg.passing_receipt(repo.cloud, "dev", commit)["reused_from"] == first["commit"]
    assert _no_lock(repo, "dev")


def test_a_failed_rehearse_step_writes_a_failed_receipt(tmp_path, repo):
    make = Make(tmp_path, repo, fail="coverage-gate")
    result = make("rehearse", "dev")
    assert result.returncode != 0
    receipts = rg.rehearse_receipts(repo.cloud, "dev")
    assert [r["result"] for r in receipts] == ["failed"]
    assert make.names() == ["_derive-before-exposure", "validate-generated", "coverage-gate"]


def test_legacy_rehearse_writes_no_receipt(make, repo):
    (repo.dev / "env.auto.tfvars").write_text('genie_space_id = "x"\n')
    result = make("rehearse", "dev")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not rg.rehearse_receipts(repo.cloud, "dev")


# -- applying runs never rewrite committed files ----------------------------------------

DERIVE_STUB = r'''#!{python}
import sys
from pathlib import Path
args = sys.argv[1:]
open({log!r}, "a").write(" ".join(args) + "\n")
config = Path(args[args.index("--config") + 1])
if "--out-dir" in args:
    config = Path(args[args.index("--out-dir") + 1]) / config.name
config.write_text(config.read_text() + "# derived from live tags\n")
'''


@pytest.mark.parametrize("mode", ["deterministic", "legacy"])
def test_derive_assignments_writes_the_overlay_in_deterministic_envs_only(tmp_path, repo, mode):
    if mode == "legacy":
        (repo.dev / "env.auto.tfvars").write_text('genie_space_id = "x"\n')
        repo.commit({})
    log = tmp_path / "derive.log"
    derive = tmp_path / "derive.py"
    derive.write_text(DERIVE_STUB.format(python=sys.executable, log=str(log)))
    derive.chmod(0o755)
    committed = repo.dev / "generated" / "abac.auto.tfvars"
    before = committed.read_text()
    environ = {k: v for k, v in os.environ.items() if k not in ("MAKEFLAGS", "MAKELEVEL")} | repo.env
    bin_dir = script_stubs.install(tmp_path / "stub-bin", {"derive_assignments.py": str(derive)})
    environ["PATH"] = f"{bin_dir}:{environ['PATH']}"
    result = subprocess.run(["make", "--no-print-directory", "derive-assignments", "ENV=dev", f"ENV_DIR={repo.dev}",
                             f"ACCOUNT_ENV_DIR={repo.cloud / 'envs' / 'account'}"], cwd=ROOT / "aws", text=True,
                            capture_output=True, env=environ)
    assert result.returncode == 0, result.stdout + result.stderr
    if mode == "deterministic":
        assert "--out-dir" in log.read_text()
        assert committed.read_text() == before
        overlay = repo.dev / "generated" / ".runtime" / "abac.auto.tfvars"
        assert overlay.read_text().endswith("# derived from live tags\n")
        assert repo.git("status", "--porcelain") == ""
    else:
        assert "--out-dir" not in log.read_text()
        assert committed.read_text().endswith("# derived from live tags\n")  # as before step 9


def test_the_same_env_split_reads_the_overlay_and_writes_nothing_committed(tmp_path, repo):
    from scripts import runtime_overlay as ro

    repo.commit({"aws/envs/dev/generated/abac.auto.tfvars":
                 'groups = {\n  "analysts" = { description = "Analysts" }\n}\n' + EMPTY_ABAC,
                 "aws/envs/dev/generated/masking_functions.sql": ""})
    ro.begin(repo.dev)
    ro.seal(repo.dev)
    environ = {k: v for k, v in os.environ.items() if k not in ("MAKEFLAGS", "MAKELEVEL")} | repo.env
    result = subprocess.run(["make", "--no-print-directory", "promote", "ENV=dev", f"ENV_DIR={repo.dev}",
                             f"ACCOUNT_ENV_DIR={repo.cloud / 'envs' / 'account'}",
                             "SOURCE_ENV=dev", f"SOURCE_ENV_DIR={repo.dev}", "DEST_ENV=", "DEST_ENV_DIR="],
                            cwd=ROOT / "aws", text=True, capture_output=True, env=environ)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo.dev / "data_access" / "abac.auto.tfvars").is_file()
    assert repo.git("status", "--porcelain") == ""


def test_a_deterministic_promotion_never_reads_prod_state_or_lists_live_policies():
    text = (ROOT / "shared" / "Makefile.shared").read_text()
    promote = text[text.index("\npromote:"):text.index("\n# make promote-to")]
    assert 'name_flags=(--state-env-dir "$$dest_env_dir" --auth' in promote
    assert ('if [ "$$($(_IS_DETERMINISTIC) "$(SOURCE_ENV_DIR)/env.auto.tfvars" 2>/dev/null)" = 1 ]; then '
            "name_flags=(--pure); fi") in promote
    assert '"$${name_flags[@]}"' in promote


def test_make_check_regenerates_without_the_overlay():
    from scripts import check_env

    seen = {}

    class Done(Exception):
        pass

    def fake_run(command, env):
        seen.update(env)
        raise Done

    scratch = check_env.Scratch.__new__(check_env.Scratch)
    scratch.cloud, scratch.make_bin = Path("/x"), "make"
    original = subprocess.run
    subprocess.run = fake_run
    try:
        with pytest.raises(Done):
            scratch.make("promote", "ENV=dev")
    finally:
        subprocess.run = original
    assert seen["GENIERAILS_NO_OVERLAY"] == "1"


@pytest.mark.parametrize("cloud", ["aws", "azure"])
@pytest.mark.parametrize("target", ["release-plan", "_release-manifest", "rehearse", "maintain"])
def test_dry_runs_of_release_targets_execute_nothing(tmp_path, repo, cloud, target):
    make = Make(tmp_path, repo, cloud)
    env = "dev" if target == "rehearse" else "prod"
    result = make(target, env, "-n", "MANIFEST=/m/manifest.json")
    assert result.returncode == 0, result.stderr
    assert make.calls() == [] and not rg.state_root(repo.cloud).exists()
    assert shlex.split  # the recipe is printed, not run


# -- the real lock opens the side-door gate for the commands that hold it ----------------

def test_rehearse_applies_dev_through_the_gate_with_its_own_lock(tmp_path, repo):
    """rehearse's real _ENV_LOCK exports its nonce; the real apply recipe it runs passes the gate."""
    make = Make(tmp_path, repo, passthrough=("apply",))
    result = make("rehearse", "dev")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "refused" not in result.stderr
    assert [c for c in make.calls() if "_apply-layer" in c]  # recorded from inside the real apply
    assert rg.passing_receipt(repo.cloud, "dev", repo.head())["applied"] is True


def test_maintain_applies_prod_governance_through_the_gate_with_its_own_lock(tmp_path, repo):
    plain = Make(tmp_path, repo)
    manifest = _planned(plain)
    assert plain("_release-manifest", "prod", f"MANIFEST={manifest}").returncode == 0
    make = Make(tmp_path, repo, passthrough=("apply-governance",))
    result = make("maintain", "prod")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "refused" not in result.stderr
    assert [c for c in make.calls() if "_apply-layer" in c and "LAYER=data_access" in c]


# -- migrate-plan ----------------------------------------------------------------------

INVENTORY_FAILS = """#!/bin/sh
echo "ERROR: external-mask inventory failed: governed_tables.json is missing" >&2
exit 2
"""
INVENTORY_FINDS = """#!/bin/sh
echo "ERROR: a mask GenieRails didn't create can resolve on a governed column" >&2
echo "  - cat.sch.t.email: rogue_mask" >&2
exit 1
"""


@pytest.mark.parametrize("inventory, expected, not_expected", [
    (INVENTORY_FAILS, "the external-mask inventory failed: governed_tables.json is missing; fix that and re-run",
     "block the cutover until they are removed"),
    (INVENTORY_FINDS, "masks GenieRails didn't create block the cutover until they are removed",
     "inventory failed"),
])
def test_migrate_plan_tells_a_failed_inventory_from_found_external_masks(make, repo, inventory, expected,
                                                                         not_expected):
    stub = make._write("inventory-case", inventory)
    result = make("migrate-plan", "prod", stubs={"external_mask_inventory.py": str(stub),
                                                 "migration.py report": "true"})
    assert result.returncode != 0, result.stdout + result.stderr  # still refused either way
    assert expected in result.stderr and not_expected not in result.stderr
    assert "ERROR:" in result.stderr  # the inventory's own output is still shown


def test_migrate_plan_passes_when_the_inventory_and_the_report_pass(make, repo):
    result = make("migrate-plan", "prod", stubs={"migration.py report": "true"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "inventory failed" not in result.stderr and "block the cutover" not in result.stderr
