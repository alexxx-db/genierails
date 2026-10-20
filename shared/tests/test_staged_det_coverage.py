"""Staged policy changes between two deterministic generations, through the coverage check in full mode.

In a native-classification env (enable_classification = true, live refresh
mode ``full``) the live refresh records the treatment tags derive-assignments
re-derived: the change's (B's). The phases before the switch (create,
tighten, restore, abandon, and rebind, judged as restore) keep the active
generation's (A's) values live, so their renders never matched the refresh,
and the coverage check accepted that only for a legacy migration's A
(scripts/coverage_gate.py). A change that retagged, newly governed or
untagged any column could then never be created, restored, abandoned or
rebound. Here: the deterministic branch (staged_generation_assignments)
accepts exactly A's recorded values, fails closed otherwise, and records the
generation it kept; the controller runs end to end with the real check; the
make targets re-split after their live refresh; and the refusals with no
GenieRails way out yet (a restore that kept newly governed columns, class.*
drift under a rebind) say which columns and what to do.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import coverage_gate as cg
from scripts import policy_generations as pg
from scripts import staged_policy_apply as spa
from scripts.runtime_overlay import effective
from tests import script_stubs
from tests.test_policy_abandon_coverage import (
    CONTROLLER, GATED_RUNNER, NEW_SP, OLD_SP, REBIND_CHECKS, _delete_principals, _promoted,
)
from tests.test_policy_rebind import GRANTS, _seed_access, access_file, grant_keys
from tests.test_staged_policy_apply import (
    Harness, _retag_live, live_policies, live_state, live_tags, released, write_env,
)

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "shared"
RETAG = {"email_partial": {"partial": "redacted"}}
DDL = "CREATE TABLE cat.sch.t (\n  email STRING,\n  contact STRING,\n  phone STRING\n);\n"
# With table grants in place (the rebind shapes) the fake state holds no deployment binding, so every
# table counts as a first exposure: only the governed column, as tests/test_policy_abandon_coverage.py.
EMAIL_DDL = "CREATE TABLE cat.sch.t (\n  email STRING\n);\n"
CI_MARKERS = ("CI", "TF_BUILD", "JENKINS_URL", "GITLAB_CI", "BUILDKITE", "CIRCLECI", "GENIERAILS_ALLOW_CI_APPLY")


def _refresh(env_dir: Path, text: str = DDL) -> None:
    """What derive-assignments leaves in full mode: live DDL and the refresh of the effective config."""
    ddl = env_dir / "ddl" / "_fetched.sql"
    ddl.parent.mkdir(exist_ok=True)
    ddl.write_text(text)
    cg.write_refresh_record(env_dir / cg.REFRESH_RELPATH, mode="full", ddl_path=ddl,
                            config_path=effective(env_dir, "abac.auto.tfvars"))


@pytest.fixture
def harness(tmp_path, monkeypatch):
    for marker in CI_MARKERS:
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.delenv("GENIERAILS_COVERAGE_GATE_RUNNER", raising=False)
    env_dir = tmp_path / "envs" / "dev"
    env_dir.mkdir(parents=True)
    h = Harness(env_dir, monkeypatch, tmp_path)
    yield h
    assert not h.invariant_problems, h.invariant_problems


def gate_on(h, tmp_path: Path, monkeypatch) -> Path:
    """The real coverage check on every stage's render (as make sets it), over the gated fake layer."""
    runner = tmp_path / "gated-runner"
    runner.write_text(GATED_RUNNER.format(python=sys.executable, inner=str(pg.RUNNER)))
    runner.chmod(0o755)
    monkeypatch.setattr(pg, "RUNNER", runner)
    monkeypatch.setenv("GENIERAILS_COVERAGE_GATE_RUNNER", str(runner))
    return runner


def gate_result(h) -> dict:
    return json.loads((h.env_dir / "data_access" / cg.GATE_FILENAME).read_text())


def change(h, old: dict, **env) -> dict:
    """Edit the config to generation B, as promote and a full-mode live refresh leave it."""
    new = write_env(h.env_dir, **({"deployer": OLD_SP} | env))
    _promoted(h, old, new)
    _refresh(h.env_dir)
    h.calls.clear()
    return new


# -- (1) the deterministic branch of the check ----------------------------------------------
class Changing:
    """A det->det change A->B journaled (create failed), with a full-mode refresh of B."""

    def __init__(self, h, **env):
        self.h, self.env_dir = h, h.env_dir
        self.old = released(h)
        self.desired = write_env(h.env_dir, **env)
        pg.save_snapshot(h.env_dir, self.desired)
        journal = spa.load_journal(h.env_dir)
        journal["transition"] = {"run_id": "r1", "from": self.old["fingerprint"],
                                 "to": self.desired["fingerprint"], "retain": "", "status": "failed",
                                 "failed_phase": "create"}
        spa.save_journal(h.env_dir, journal)
        _refresh(h.env_dir)

    def render(self, stage: str, keep_new_tags: bool = False) -> Path:
        rendered = pg.plan_stage(stage, self.desired, self.old, None, keep_new_tags=keep_new_tags)["rendered"]
        return pg.write_stage(self.env_dir / "data_access", rendered) / "abac.auto.tfvars"

    def judge(self, tfvars: Path, stage: str, *, keep_new_tags: bool = False, old: str | None = None, **extra):
        staged = {"stage": stage, "old_fingerprint": self.old["fingerprint"] if old is None else old,
                  "keep_new_tags": keep_new_tags, **extra}
        return cg._live_refresh(self.env_dir, tfvars, staged)


@pytest.fixture
def retagging(harness):
    return Changing(harness, versions=RETAG)


@pytest.fixture
def adding(harness):
    """B newly governs a column (A doesn't): the newly classified column shape."""
    return Changing(harness, columns=("email", "contact"))


def test_the_plain_check_refuses_a_det_to_det_create_render(retagging):
    """The bug's precondition: the create render keeps A's values; the refresh recorded B's."""
    tfvars = retagging.render("create")
    assert pg._load(tfvars)["tag_assignments"] != pg._load(effective(retagging.env_dir, "abac.auto.tfvars"))[
        "tag_assignments"]
    assert cg.live_refresh(retagging.env_dir, tfvars)[0] is None


@pytest.mark.parametrize("stage", ["create", "tighten", "restore", "abandon"])
def test_the_phases_that_keep_the_active_generation_pass_and_record_it(retagging, stage):
    keep = stage == "restore"
    refreshed_at, detail, kept = retagging.judge(retagging.render(stage, keep), stage, keep_new_tags=keep)
    assert refreshed_at is not None, detail
    fp = retagging.old["fingerprint"]
    assert kept == {"kept_from": fp, "uncovered_columns": []}
    assert f"{stage} keeps generation {fp}'s values (kept_from {fp})" in detail


def test_newly_governed_columns_wait_for_the_switch_before_it_and_keep_their_value_after(adding):
    refreshed_at, detail, kept = adding.judge(adding.render("create"), "create")
    assert refreshed_at is not None, detail
    assert kept["uncovered_columns"] == ["cat.sch.t.contact"] and "1 column(s) the live refresh governs" in detail
    # After an attempted switch the restore keeps them on B's value: here that is the refresh exactly.
    refreshed_at, detail, kept = adding.judge(adding.render("restore", True), "restore", keep_new_tags=True)
    assert (refreshed_at is not None, detail, kept) == (True, "full", {})
    # Abandon never widens to them (plan_stage), whatever KEEP_NEW_TAGS says.
    refreshed_at, detail, kept = adding.judge(adding.render("abandon"), "abandon", keep_new_tags=True)
    assert refreshed_at is not None and kept["uncovered_columns"] == ["cat.sch.t.contact"], detail


@pytest.mark.parametrize("stage", ["switch", "retire"])
def test_switch_and_retire_still_need_the_refresh_exactly(retagging, stage):
    assert retagging.judge(retagging.render("switch"), "switch")[0] is not None
    refreshed_at, detail, _ = retagging.judge(retagging.render("create"), stage)
    assert refreshed_at is None and f"the {stage} phase applies the desired values" in detail


def _edit_snapshot(c):
    path = pg.ledger_dir(c.env_dir) / f"{c.old['fingerprint']}.json"
    column = next(iter(c.old["tags"]))
    pg.write_json(path, c.old | {"tags": {column: c.desired["tags"][column]}})


def _journal(c, **edit):
    journal = spa.load_journal(c.env_dir)
    journal.update({k: v for k, v in edit.items() if k != "transition"})
    if "transition" in edit:
        journal["transition"] = None if edit["transition"] is None else journal["transition"] | edit["transition"]
    spa.save_journal(c.env_dir, journal)


@pytest.mark.parametrize("forge, reason", [
    (_edit_snapshot, "doesn't match its fingerprint"),
    (lambda c: (pg.ledger_dir(c.env_dir) / f"{c.old['fingerprint']}.json").unlink(), "no snapshot"),
    (lambda c: _journal(c, active="0" * 16), "records no transition from"),
    (lambda c: _journal(c, transition={"from": "0" * 16}), "records no transition from"),
    (lambda c: _journal(c, transition=None), "records no transition from"),
    (lambda c: spa.journal_path(c.env_dir).unlink(), "records no transition from"),
    (lambda c: _journal(c, transition={"to": "0" * 16}), "records no transition from"),
])
def test_a_forged_or_missing_journal_or_snapshot_refuses(retagging, forge, reason):
    tfvars = retagging.render("create")
    forge(retagging)
    refreshed_at, detail, kept = retagging.judge(tfvars, "create")
    assert refreshed_at is None and reason in detail and kept == {}, detail
    assert "keeps the active generation's recorded values" in detail


def test_abandon_drops_a_transition_the_config_moved_on_from(retagging):
    tfvars = retagging.render("abandon")
    _journal(retagging, transition={"to": "0" * 16})
    assert retagging.judge(tfvars, "abandon")[0] is not None


def test_a_render_with_any_other_value_refuses(retagging):
    tfvars = retagging.render("create")
    value = next(iter(retagging.old["tags"].values()))
    tfvars.write_text(tfvars.read_text().replace(f'"{value}"', f'"{value}x"'))
    refreshed_at, detail, _ = retagging.judge(tfvars, "create")
    assert refreshed_at is None and "neither the live refresh's nor the values generation" in detail


def test_a_refresh_of_another_config_refuses(retagging):
    tfvars = retagging.render("create")
    record = json.loads((retagging.env_dir / cg.REFRESH_RELPATH).read_text())
    (retagging.env_dir / cg.REFRESH_RELPATH).write_text(json.dumps(record | {"tag_assignments_sha256": "0" * 64}))
    refreshed_at, detail, _ = retagging.judge(tfvars, "create")
    assert refreshed_at is None and "generated config's tag_assignments don't match" in detail


def test_a_phase_that_opens_access_must_cover_every_column_the_refresh_governs(adding):
    """The grant re-check and the rebind phases have no allowance for an untagged governed column."""
    tfvars = adding.render("restore")
    assert adding.judge(tfvars, "restore")[0] is not None  # restore itself refuses business grant creates
    for extra in ({"opens_access": True}, {"rebind_fingerprint": adding.desired["fingerprint"]}):
        refreshed_at, detail, _ = adding.judge(tfvars, "restore", **extra)
        assert refreshed_at is None and "cat.sch.t.contact" in detail and "opens access" in detail, detail


def test_a_rebind_is_judged_as_restore_against_the_generation_it_re_points_to(retagging):
    tfvars = retagging.render("restore", True)
    fp = retagging.desired["fingerprint"]
    assert retagging.judge(tfvars, "restore", keep_new_tags=True, rebind_fingerprint=fp)[0] is not None
    refreshed_at, detail, _ = retagging.judge(tfvars, "restore", keep_new_tags=True, rebind_fingerprint="0" * 16)
    assert refreshed_at is None and "which the rebind re-points to" in detail
    refreshed_at, detail, _ = retagging.judge(tfvars, "abandon", rebind_fingerprint=fp)
    assert refreshed_at is None and "judged as the restore" in detail


def test_a_rebind_whose_config_differs_from_the_change_beyond_principals_refuses(retagging):
    """rebind_problems must be clean: here the generated config adds a column the change didn't have."""
    tfvars = retagging.render("restore", True)
    write_env(retagging.env_dir, versions=RETAG, columns=("email", "contact"))
    _refresh(retagging.env_dir)
    fp = pg.desired_generation(retagging.env_dir)["fingerprint"]
    refreshed_at, detail, _ = retagging.judge(tfvars, "restore", keep_new_tags=True, rebind_fingerprint=fp)
    assert refreshed_at is None and "with only its principals moved" in detail and "change their treatment" in detail


def test_the_gate_cli_passes_the_rebind_and_access_flags():
    args = []
    real = cg.run_gate

    def capture(*a, **_):
        args.append(a[-1])
        return 0

    cg.run_gate = capture
    try:
        cg.main(["run", "--env-dir", "/tmp", "--env-name", "dev", "--layer-config-dir", "/tmp", "--stage", "restore",
                 "--old-fingerprint", "a" * 16, "--keep-new-tags", "--rebind-fingerprint", "b" * 16,
                 "--opens-access"])
    finally:
        cg.run_gate = real
    assert args == [{"stage": "restore", "old_fingerprint": "a" * 16, "keep_new_tags": True,
                     "rebind_fingerprint": "b" * 16, "opens_access": True}]


# -- (2)-(4) the controller with the real check in full mode ----------------------------------
def test_a_det_to_det_retag_runs_create_switch_and_grant_through_the_check(harness, tmp_path, monkeypatch, capfd):
    """Fails on main: the create hook's check refused (the active generation isn't a legacy migration's)."""
    old = released(harness, deployer=OLD_SP)
    gate_on(harness, tmp_path, monkeypatch)
    new = change(harness, old, versions=RETAG)
    capfd.readouterr()
    assert harness.apply() == 0, "".join(capfd.readouterr())
    out = "".join(capfd.readouterr())
    assert harness.calls == ["create", "inventory:create/restore", "verify-access:create", "switch",
                             "inventory:switch", "verify-access:switch", "grant"]
    assert f"create keeps generation {old['fingerprint']}'s values (kept_from {old['fingerprint']})" in out
    assert harness.journal()["active"] == new["fingerprint"] and harness.journal()["transition"] is None
    assert live_tags(live_state(harness.env_dir)) == new["tags"]
    # The grant re-check judged the switch render: the refresh's own values, nothing kept.
    gate = gate_result(harness)
    assert gate["status"] == "pass" and "kept_from" not in gate


def test_a_newly_classified_column_alone_runs_through_the_check(harness, tmp_path, monkeypatch, capfd):
    old = released(harness, deployer=OLD_SP)
    gate_on(harness, tmp_path, monkeypatch)
    new = change(harness, old, columns=("email", "contact"))
    assert new["tags"].keys() - old["tags"].keys() == {"cat.sch.t.contact"}
    capfd.readouterr()
    assert harness.apply() == 0, "".join(capfd.readouterr())
    out = "".join(capfd.readouterr())
    assert "stay untagged in this phase, which opens no access (cat.sch.t.contact)" in out
    assert live_tags(live_state(harness.env_dir)) == new["tags"]


def test_a_failed_switch_restores_through_the_check_and_then_resumes(harness, tmp_path, monkeypatch):
    old = released(harness, deployer=OLD_SP)
    gate_on(harness, tmp_path, monkeypatch)
    new = change(harness, old, versions=RETAG)
    harness.fail_verify = {"switch"}
    assert harness.apply() == 1
    t = harness.journal()["transition"]
    assert t["restored"] and not t["restore_failed"] and not t["restoring"], t
    assert live_tags(live_state(harness.env_dir)) == old["tags"]
    gate = gate_result(harness)
    assert gate["status"] == "pass" and gate["kept_from"] == old["fingerprint"]
    assert pg.load_stage_record(harness.env_dir)["stage"] == "restore"
    # The next run resumes (no restore_failed loop) and finishes.
    harness.calls.clear()
    assert harness.apply() == 0
    assert "switch" in harness.calls and harness.journal()["active"] == new["fingerprint"]


def test_abandon_of_a_det_to_det_change_that_never_switched_passes_the_check(harness, tmp_path, monkeypatch):
    old = released(harness, deployer=OLD_SP)
    gate_on(harness, tmp_path, monkeypatch)
    new = change(harness, old, versions=RETAG)
    harness.fail_verify = {"create"}
    assert harness.apply() == 1
    harness.calls.clear()
    assert harness.controller().abandon() == 0
    assert gate_result(harness)["kept_from"] == old["fingerprint"]
    assert harness.journal()["transition"] is None and harness.journal()["active"] == old["fingerprint"]
    assert {p["name"] for p in live_policies(live_state(harness.env_dir))} == {p["name"] for p in old["fgac_policies"]}
    assert new["fingerprint"] != old["fingerprint"]


# -- (5)-(6) make policy-abandon and policy-rebind, full mode -----------------------------------
# derive-assignments in a deterministic native-classification env: make has begun the overlay
# (a copy of generated/, which stands in for the model re-derived from live class.* tags), and
# the refresh records the overlay's tags, as the real script does.
DERIVE = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, {shared!r})
from scripts import coverage_gate as cg

args = sys.argv[1:]
ddl, record = Path(args[args.index("--write-ddl") + 1]), Path(args[args.index("--refresh-record") + 1])
ddl.parent.mkdir(exist_ok=True)
ddl.write_text({ddl!r})
assert "--ddl-only" not in args, "a native-classification env refreshes in full mode"
config = Path(args[args.index("--out-dir") + 1]) / "abac.auto.tfvars"
cg.write_refresh_record(record, mode="full", ddl_path=ddl, config_path=config)
with open(os.environ["DERIVE_LOG"], "a") as log:
    log.write(json.dumps({{"args": args, "config": str(config)}}) + "\n")
'''


def classify(h) -> None:
    """A native-classification env: make's live refresh is derive-assignments (full mode)."""
    with (h.env_dir / "env.auto.tfvars").open("a") as f:
        f.write("enable_classification = true\n")


def make(target: str, h, tmp_path: Path, runner: Path, ddl: str = DDL, **stubs: str) -> subprocess.CompletedProcess:
    controller, derive = tmp_path / "controller.py", tmp_path / "derive.py"
    controller.write_text(CONTROLLER.format(shared=str(SHARED)))
    derive.write_text(DERIVE.format(shared=str(SHARED), ddl=ddl))
    bin_dir = script_stubs.install(tmp_path / "stub-bin", {
        "staged_policy_apply.py": script_stubs.python_file(controller),
        "derive_assignments.py": script_stubs.python_file(derive),
        "external_mask_inventory.py": "true", **stubs})
    (tmp_path / "harness").mkdir(exist_ok=True)
    environ = {k: v for k, v in script_stubs.without_ci(os.environ).items()
               if k not in ("MAKEFLAGS", "MAKELEVEL", "APPLY_FLAGS", "GENIERAILS_COVERAGE_GATE_RUNNER")}
    environ |= {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HARNESS_TMP": str(tmp_path / "harness"),
                "GATED_RUNNER": str(runner), "DERIVE_LOG": str(tmp_path / "derive.log"),
                "GIT_CONFIG_GLOBAL": "/dev/null"}
    return subprocess.run(
        ["make", "--no-print-directory", target, "ENV=dev", f"ENV_DIR={h.env_dir}", f"ROOT_RUNNER={runner}"],
        cwd=ROOT / "aws", text=True, capture_output=True, env=environ, timeout=600)


def derives(tmp_path: Path) -> list[dict]:
    log = tmp_path / "derive.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.is_file() else []


def rolled_back(h, tmp_path, monkeypatch, *, deployer: str = OLD_SP, old_deployer: str = OLD_SP,
                **env) -> tuple[dict, dict, Path]:
    """A det->det change whose switch failed and was restored, in a native-classification env."""
    old = released(h, deployer=old_deployer)
    runner = gate_on(h, tmp_path, monkeypatch)
    new = write_env(h.env_dir, deployer=deployer, **env)
    classify(h)
    _promoted(h, old, new)
    _refresh(h.env_dir)
    h.fail_verify = {"switch"}
    assert h.apply() == 1
    assert h.journal()["transition"]["restored"]
    h.calls.clear()
    return old, new, runner


def test_make_policy_abandon_in_full_mode_refreshes_the_overlay_and_drops_the_change(harness, tmp_path,
                                                                                    monkeypatch):
    old, new, runner = rolled_back(harness, tmp_path, monkeypatch, versions=RETAG)
    t = harness.journal()["transition"]
    result = make("policy-abandon", harness, tmp_path, runner)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    # The live refresh was full mode and recorded the overlay (B's tags), then promote re-split it.
    logged = derives(tmp_path)
    assert len(logged) == 1 and logged[0]["config"].endswith("generated/.runtime/abac.auto.tfvars"), logged
    assert "=== Derive Assignments (dev) ===" in out and "=== Split generated/ to account" in out
    record = json.loads((harness.env_dir / cg.REFRESH_RELPATH).read_text())
    assert record["mode"] == "full" and record["tag_assignments_sha256"] == cg.tag_assignments_digest(
        pg._load(effective(harness.env_dir, "abac.auto.tfvars"))["tag_assignments"])
    gate = gate_result(harness)
    assert gate["status"] == "pass" and gate["kept_from"] == old["fingerprint"]
    assert f"abandon keeps generation {old['fingerprint']}'s values" in out
    assert f"Abandoned generation {t['to']}" in out
    assert harness.journal()["transition"] is None and harness.journal()["active"] == old["fingerprint"]
    assert live_tags(live_state(harness.env_dir)) == old["tags"]
    assert new["fingerprint"] == t["to"]


def deleted_and_recreated(h, tmp_path, monkeypatch, **env):
    """rolled_back, then the principals are deleted and the deployer recreated under a new ID."""
    # The previous generation names no deployer the rebind replaces (rebind_problems).
    old, failed, runner = rolled_back(h, tmp_path, monkeypatch, old_deployer="", versions=RETAG, **env)
    _seed_access(h, OLD_SP)
    _delete_principals(h)
    fixed = write_env(h.env_dir, versions=RETAG, deployer=NEW_SP, **env)
    classify(h)
    _promoted(h, old, failed, fixed)
    _refresh(h.env_dir, EMAIL_DDL)
    h.calls.clear()
    return old, failed, fixed, runner


def test_make_policy_rebind_then_abandon_in_full_mode(harness, tmp_path, monkeypatch):
    old, failed, fixed, runner = deleted_and_recreated(harness, tmp_path, monkeypatch)
    result = make("policy-rebind", harness, tmp_path, runner, EMAIL_DDL, **REBIND_CHECKS)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert derives(tmp_path) and "=== Split generated/ to account" in out
    t = harness.journal()["transition"]
    assert t["rebound"]["generation"] == fixed["fingerprint"]
    gate = gate_result(harness)
    assert gate["status"] == "pass" and gate["kept_from"] == old["fingerprint"] and gate["uncovered_columns"] == []
    assert grant_keys(live_state(harness.env_dir)) == set(GRANTS)
    assert live_tags(live_state(harness.env_dir)) == old["tags"]

    result = make("policy-abandon", harness, tmp_path, runner, EMAIL_DDL)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert harness.journal()["transition"] is None and harness.journal()["active"] == old["fingerprint"]
    assert grant_keys(live_state(harness.env_dir)) == set(GRANTS)


def test_rebind_with_a_column_classified_after_the_change_refuses_and_grants_nothing(harness, tmp_path,
                                                                                     monkeypatch):
    """S4 under rebind: the overlay governs a column the change didn't, so it isn't the change with only
    principals moved. The refusal names the column and the way out; nothing is applied, no grant made."""
    old, failed, _, runner = deleted_and_recreated(harness, tmp_path, monkeypatch)
    drifted = write_env(harness.env_dir, versions=RETAG, deployer=NEW_SP, columns=("email", "phone"),
                        classes=("email_address",))
    classify(harness)
    _promoted(harness, old, failed, drifted)
    before = (harness.journal(), live_state(harness.env_dir), access_file(harness).read_text())
    result = make("policy-rebind", harness, tmp_path, runner, EMAIL_DDL, **REBIND_CHECKS)
    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert "1 column(s) have another treatment in the generated config than in the unfinished change" in out
    assert "cat.sch.t.phone" in out and "class.* tags" in out and "re-run make policy-rebind ENV=dev" in out
    # Nothing planned or applied: the lost grants are still lost (fake_access.json's "gone").
    assert (harness.journal(), live_state(harness.env_dir), access_file(harness).read_text()) == before
    assert "table_access|cat.sch.t|raw" in json.loads(access_file(harness).read_text())["gone"]


# -- (7)-(8) the refusals with no GenieRails way out yet --------------------------------------
def test_rebind_of_a_restore_that_kept_a_newly_governed_column_refuses_naming_it(harness, tmp_path, monkeypatch):
    """S3 with recreated principals: the restore kept B's new column on B's value. Abandon and rebind
    both refuse, naming the column and the one way out (resume), before anything is applied."""
    old = released(harness, deployer=OLD_SP)
    failed = write_env(harness.env_dir, deployer=OLD_SP, columns=("email", "contact"))
    harness.fail_verify = {"switch"}
    assert harness.apply() == 1
    t = harness.journal()["transition"]
    assert "cat.sch.t.contact" in t["restored"]["tags"]
    _seed_access(harness, OLD_SP)
    _delete_principals(harness)
    write_env(harness.env_dir, deployer=NEW_SP, columns=("email", "contact"))
    harness.calls.clear()
    before = (harness.journal(), live_state(harness.env_dir))
    for command, action in (("rebind", "re-pointed"), ("abandon", "abandoned")):
        with pytest.raises(spa.Refused) as refused:
            getattr(harness.controller(), command)()
        message = str(refused.value)
        assert f"can't be {action}: 1 column(s) keep the new generation's treatment (cat.sch.t.contact)" in message
        assert f"resume the change by re-running the command that started it (commit abc123) with the generated " \
               f"config on generation {failed['fingerprint']}" in message
        assert "Changes that can't be dropped" in message and "Nothing was applied" in message
    assert harness.calls == [] and (harness.journal(), live_state(harness.env_dir)) == before
    assert live_tags(live_state(harness.env_dir))["cat.sch.t.contact"] == failed["tags"]["cat.sch.t.contact"]
    assert old["tags"].keys() == {"cat.sch.t.email"}


def test_resume_and_abandon_after_class_drift_refuse_naming_the_drifted_columns(harness, tmp_path, monkeypatch):
    """S4: a column is classified after B started. Resume and abandon both refuse before any plan, naming
    it and the way out (put the class.* tags back); nothing is planned, applied or recorded."""
    old = released(harness, deployer=OLD_SP)
    gate_on(harness, tmp_path, monkeypatch)
    new = change(harness, old, versions=RETAG)
    harness.fail_verify = {"create"}
    assert harness.apply() == 1
    drifted = write_env(harness.env_dir, versions=RETAG, deployer=OLD_SP, columns=("email", "phone"))
    _promoted(harness, old, new, drifted)
    _refresh(harness.env_dir)
    gate_file = harness.env_dir / "data_access" / cg.GATE_FILENAME
    before = (harness.journal(), live_state(harness.env_dir), gate_file.read_bytes())
    harness.calls.clear()
    with pytest.raises(spa.Refused) as refused:
        harness.apply()
    message = str(refused.value)
    assert "1 column(s) have another treatment in the generated config than in generation" in message
    assert "(cat.sch.t.phone)" in message and "live class.* tags changed" in message
    assert "make policy-abandon refuses too" in message and "or drop it with make policy-abandon" not in message
    assert "put those columns' class.* tags (or the treatment settings) back as they were when the change " \
           "started, then resume it or run make policy-abandon ENV=dev" in message
    assert harness.calls == []
    assert (harness.journal(), live_state(harness.env_dir), gate_file.read_bytes()) == before

    with pytest.raises(spa.Refused) as refused:
        harness.controller().abandon()
    message = str(refused.value)
    assert f"this change can't be abandoned: 1 column(s) have another treatment in the generated config than " \
           f"in the unfinished change {new['fingerprint']} (cat.sch.t.phone)" in message
    assert "live class.* tags changed" in message and "nothing was applied" in message
    assert "put those columns' class.* tags (or the treatment settings) back as they were when the change " \
           "started, then re-run make policy-abandon ENV=dev" in message
    assert "Changes that can't be dropped" in message
    # No plan, no apply, no gate re-run: the journal, the live state and the check's result are as before.
    assert harness.calls == []
    assert (harness.journal(), live_state(harness.env_dir), gate_file.read_bytes()) == before
    assert harness.journal()["transition"]["to"] == new["fingerprint"]
    assert not (pg.ledger_dir(harness.env_dir) / "abandon").exists()


# -- (9) the split's non-treatment assignments vs the generated config's ------------------------
def _with_table_tag(path: Path, value: str) -> None:
    text = path.read_text()
    tag = (f'  {{\n    entity_type = "tables"\n    entity_name = "cat.sch.t"\n    tag_key = "domain"\n'
           f'    tag_value = "{value}"\n  }},\n')
    path.write_text(text.replace("tag_assignments = [\n", "tag_assignments = [\n" + tag, 1))


@pytest.mark.parametrize("command", ["abandon", "rebind"])
def test_a_split_that_differs_from_the_generated_config_is_refused_before_any_plan(harness, tmp_path, monkeypatch,
                                                                                   command):
    if command == "rebind":
        _, failed, _, _ = deleted_and_recreated(harness, tmp_path, monkeypatch)
    else:
        rolled_back(harness, tmp_path, monkeypatch, versions=RETAG)
    _with_table_tag(effective(harness.env_dir, "abac.auto.tfvars"), "finance")
    _with_table_tag(harness.env_dir / "data_access" / "abac.auto.tfvars", "hr")
    before = (harness.journal(), live_state(harness.env_dir))
    with pytest.raises(spa.Refused) as refused:
        getattr(harness.controller(), command)()
    message = str(refused.value)
    assert "non-treatment tag assignments differ from the generated config's" in message
    assert "tables|cat.sch.t|domain|finance is in the generated config only" in message
    assert "tables|cat.sch.t|domain|hr is in the split data_access config only" in message
    assert f"Run make promote ENV=dev to re-split it (make policy-{command} promotes" in message
    assert harness.calls == [] and (harness.journal(), live_state(harness.env_dir)) == before


def test_split_assignment_problems_ignores_treatment_tags(harness):
    write_env(harness.env_dir)
    assert pg.split_assignment_problems(harness.env_dir) == []
    write_env(harness.env_dir, versions=RETAG)
    (harness.env_dir / "data_access" / "abac.auto.tfvars").write_text(
        (harness.env_dir / "data_access" / "abac.auto.tfvars").read_text())
    layer = harness.env_dir / "data_access" / "abac.auto.tfvars"
    layer.write_text(layer.read_text().replace("email_partial", "email_other"))
    assert pg.split_assignment_problems(harness.env_dir) == []


# -- (10) out-of-band retags still refuse, with the check on -----------------------------------
def test_rebind_refuses_a_column_retagged_live_with_the_check_on(harness, tmp_path, monkeypatch, capfd):
    old, failed, _, _ = deleted_and_recreated(harness, tmp_path, monkeypatch)
    column = next(iter(old["tags"]))
    _retag_live(harness.env_dir, column, failed["tags"][column])
    harness.live_check = "real"
    before = (harness.journal(), live_state(harness.env_dir))
    capfd.readouterr()
    with pytest.raises(spa.Refused, match="rebind-protect plan was refused"):
        harness.controller().rebind()
    err = capfd.readouterr().err
    assert f'treatment["columns|{column}|gr_treatment"]: update' in err
    assert harness.calls == ["rebind-protect:plan"]
    assert (harness.journal(), live_state(harness.env_dir)) == before


@pytest.mark.parametrize("when", ["before-the-plan", "between-the-plan-and-the-live-check"])
def test_abandon_refuses_a_column_retagged_live_with_the_check_on(harness, tmp_path, monkeypatch, capfd, when):
    from tests.test_staged_policy_apply import _after_call

    old, new, _ = rolled_back(harness, tmp_path, monkeypatch, versions=RETAG)
    (column, value), = ((c, v) for c, v in new["tags"].items() if v != old["tags"].get(c))
    if when == "before-the-plan":
        _retag_live(harness.env_dir, column, value)
        expect = "abandon plan was refused"
    else:
        _after_call(harness, "abandon:plan", lambda env: _retag_live(harness.env_dir, column, value))
        expect = r"can't be abandoned \(the live check found"
    harness.live_check = "real"
    journal, policies = harness.journal(), live_policies(live_state(harness.env_dir))
    capfd.readouterr()
    with pytest.raises(spa.Refused, match=expect):
        harness.controller().abandon()
    if when == "before-the-plan":
        err = capfd.readouterr().err
        assert f"columns|{column}|gr_treatment" in err
        assert harness.calls == ["abandon:plan"]
        # The check passed on the abandon render (A's values kept); the plan is what refused.
        assert gate_result(harness)["status"] == "pass"
    else:
        assert harness.calls == ["abandon:plan", "inventory:restore"]
    assert harness.journal() == journal and live_policies(live_state(harness.env_dir)) == policies
