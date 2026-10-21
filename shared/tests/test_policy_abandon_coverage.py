"""make policy-abandon re-runs the coverage check on the abandon render.

The live AWS shape (main 7c0e62f): make policy-rebind passed, then make
policy-abandon failed its plan with ``Coverage check stale`` on the 9 kept
table SELECT grants. The last check had judged the rebind render (22
policies); the abandon render drops the change's 15 policies and leaves the 7
restored ones, so the fingerprint Terraform computed no longer matched.
Every other stage re-runs the check on its own render
(GENIERAILS_COVERAGE_GATE_RUNNER, policy_generations._rerun_coverage_gate);
make policy-abandon didn't pass the runner, so the abandon hook never did.

Runs on the rebind harness (tests/test_policy_rebind.py) with the real
coverage check (scripts/coverage_gate.py, validate_abac.py --coverage-gate)
and a data_access runner that models the module's precondition for this case.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import coverage_gate as cg
from scripts import policy_generations as pg
from scripts import staged_policy_apply as spa
from tests import script_stubs
from tests.test_policy_rebind import GRANTS, VERSIONS, _seed_access, access_file, harness  # noqa: F401
from tests.test_staged_policy_apply import live_policies, live_state, released, write_env

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "shared"
DDL = "CREATE TABLE cat.sch.t (\n  email STRING\n);\n"
# Deployer SPs as application IDs: the real coverage check validates them.
OLD_SP, NEW_SP = "11111111-0000-4000-8000-000000000001", "22222222-0000-4000-8000-000000000002"
DELETED = {"raw", "regional", "masked", OLD_SP}

# terraform_layer.sh for data_access over the fake layer (tests/test_staged_policy_apply.py):
# console answers coverage_gate_inputs from the config Terraform would read (the staged
# render, the table grants, the DDL); plan first applies modules/data_access's check for
# kept table SELECT whose ABAC policy is removed: it needs a pass for exactly these inputs.
GATED_RUNNER = r'''#!{python}
import base64, hashlib, json, os, sys
layer, env_name, command, *args = sys.argv[1:]
layer_dir = os.environ["LAYER_ENV_DIR"]
config = os.environ.get("GENIERAILS_LAYER_CONFIG_DIR") or layer_dir
access = os.path.join(layer_dir, "fake_access.json")
grants = sorted(g.split("|", 1)[1] for g in (json.load(open(access))["grants"] if os.path.exists(access) else [])
                if g.startswith("table_access|"))
digest = hashlib.sha256()
for path in (os.path.join(config, "abac.auto.tfvars"), os.path.join(config, "masking_functions.sql"),
             os.path.join(layer_dir, "..", "ddl", "_fetched.sql")):
    digest.update(open(path, "rb").read() if os.path.exists(path) else b"")
digest.update(json.dumps(grants).encode())
fingerprint = digest.hexdigest()
if command == "console":
    inputs = {{"fingerprint": fingerprint, "grant_tables": sorted({{g.split("|")[0] for g in grants}}),
              "acknowledged_columns": [], "grant_keys": grants, "max_age": "24h"}}
    print("+ terraform console")
    print('"' + base64.b64encode(json.dumps(inputs).encode()).decode() + '"')
    sys.exit(0)
if command == "plan":
    import hcl2
    state_path = os.path.join(layer_dir, "terraform.tfstate")
    state = json.load(open(state_path)) if os.path.exists(state_path) else {{"resources": []}}
    applied = {{(r["type"], r["name"]): [i["index_key"] for i in r["instances"]] for r in state["resources"]}}
    rendered = {{p["name"] for p in hcl2.loads(open(os.path.join(config, "abac.auto.tfvars")).read())
                .get("fgac_policies") or []}}
    removed = set(applied.get(("databricks_policy_info", "policies"), [])) - rendered
    kept = applied.get(("databricks_grant", "table_access"), [])
    gate_path = os.path.join(layer_dir, ".coverage_gate.json")
    gate = json.load(open(gate_path)) if os.path.exists(gate_path) else None
    status = ("missing" if gate is None else "stale" if gate.get("fingerprint") != fingerprint
              else "pass" if gate.get("status") == "pass" else "failed")
    if removed and kept and status != "pass":
        print("Error: Resource precondition failed", file=sys.stderr)
        print(f"local.coverage_gate_status is {{status!r}}", file=sys.stderr)
        print(f"local.table_grants_needing_gate is tuple with {{len(kept)}} elements", file=sys.stderr)
        problem = {{"missing": "no coverage check result exists", "failed": "the last coverage check FAILED",
                   "stale": "the inputs changed after the coverage check ran"}}[status]
        print(f"Coverage check {{status}}: {{problem}}", file=sys.stderr)
        sys.exit(1)
os.execv(sys.executable, [sys.executable, {inner!r}, *sys.argv[1:]])
'''

# Below make as scripts/staged_policy_apply.py: the real Controller on the harness, reading
# the environment make gives it (tests/test_staged_migration_coverage.py does the same for rehearse).
CONTROLLER = r'''
import os, sys
from pathlib import Path
sys.path.insert(0, {shared!r})
import pytest
from scripts import policy_generations as pg, staged_policy_apply as spa
from tests.test_staged_policy_apply import Harness

args = sys.argv[1:]
env_dir = Path(args[args.index("--env-dir") + 1])
h = Harness(env_dir, pytest.MonkeyPatch(), Path(os.environ["HARNESS_TMP"]))
pg.RUNNER = Path(os.environ["GATED_RUNNER"])
try:
    rc = getattr(h.controller(), args[0])()
except spa.Refused as exc:
    print(f"ERROR: staged policy apply refused: {{exc}}", file=sys.stderr)
    rc = 3
if h.invariant_problems:
    print(h.invariant_problems, file=sys.stderr)
    rc = rc or 1
sys.exit(rc)
'''


# Below make as scripts/derive_assignments.py (make's live refresh before the staged
# controller): writes the live DDL and the refresh record as the real script does, and
# logs whether the env lock was held, without reading Unity Catalog.
DERIVE = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, {shared!r})
from scripts import coverage_gate as cg, environment_lock

args = sys.argv[1:]
ddl, record = Path(args[args.index("--write-ddl") + 1]), Path(args[args.index("--refresh-record") + 1])
env_dir = Path.cwd()
ddl.parent.mkdir(exist_ok=True)
ddl.write_text({ddl!r})
config = None if "--ddl-only" in args else Path(args[args.index("--config") + 1])
cg.write_refresh_record(record, mode="ddl" if config is None else "full", ddl_path=ddl, config_path=config)
with open(os.environ["DERIVE_LOG"], "a") as log:
    log.write(json.dumps({{"args": args, "locked": environment_lock.lock_path(env_dir).is_file()}}) + "\n")
'''


def _refresh(env_dir: Path, config: Path) -> None:
    """What derive-assignments leaves: live DDL and a refresh of ``config``'s tags."""
    ddl = env_dir / "ddl" / "_fetched.sql"
    ddl.parent.mkdir(exist_ok=True)
    ddl.write_text(DDL)
    cg.write_refresh_record(env_dir / cg.REFRESH_RELPATH, mode="full", ddl_path=ddl, config_path=config)


def _gated(h, tmp_path: Path, monkeypatch) -> Path:
    """Install the gated runner as the hooks' terraform_layer.sh; return it (make's ROOT_RUNNER)."""
    runner = tmp_path / "gated-runner"
    runner.write_text(GATED_RUNNER.format(python=sys.executable, inner=str(pg.RUNNER)))
    runner.chmod(0o755)
    monkeypatch.setattr(pg, "RUNNER", runner)
    return runner


def _abandon_render(h) -> dict:
    t = h.journal()["transition"]
    changed, old = (pg.load_snapshot(h.env_dir, fp) for fp in (t["to"], t["from"]))
    return pg.plan_stage("abandon", changed, old, None, keep_new_tags=t["switch_attempted"])["rendered"]


GROUPS = "groups = {\n  raw = {}\n  regional = {}\n  masked = {}\n}\n\n"


def _promoted(h, *generations) -> None:
    """The split config as promote leaves it: the account groups, and tag policies in the account layer.

    The generated config carries the groups too, so a promote under make (policy-abandon and
    policy-rebind re-split after their live refresh) splits the same config out again.
    """
    generated = h.env_dir / "generated" / "abac.auto.tfvars"
    if not generated.read_text().startswith(GROUPS):
        generated.write_text(GROUPS + generated.read_text())
    values = sorted({v for g in generations for v in g["tags"].values()})
    account = h.env_dir.parent / "account"
    account.mkdir(exist_ok=True)
    (account / "abac.auto.tfvars").write_text(
        'tag_policies = [\n  {\n    key = "gr_treatment"\n    description = "GenieRails treatment"\n'
        f"    values = {json.dumps(values)}\n  }}\n]\n")
    layer = h.env_dir / "data_access" / "abac.auto.tfvars"
    rest = layer.read_text().split("tag_assignments = [", 1)[1]
    layer.write_text(GROUPS + "tag_assignments = [" + rest)


def _delete_principals(h) -> None:
    """tests/test_policy_rebind.py's SCIM cleanup, for the application-ID deployer."""
    path = h.env_dir / "data_access" / "terraform.tfstate"
    state = live_state(h.env_dir)
    for policy in state["resources"]:
        if policy["type"] == "databricks_policy_info":
            for instance in policy["instances"]:
                for field in ("to_principals", "except_principals"):
                    instance["attributes"][field] = ["unknown" if p in DELETED else p
                                                     for p in instance["attributes"][field]]
    path.write_text(json.dumps(state))
    gone = [g for g in GRANTS if g.rsplit("|", 1)[-1] in DELETED or g.startswith("terraform_sp_manage_catalog|")]
    access_file(h).write_text(json.dumps({"grants": GRANTS, "client_id": NEW_SP, "gone": gone}))


def _before_rebind(h) -> None:
    """A rolled-back change whose principals were deleted and recreated: what make policy-rebind fixes."""
    old = released(h)
    failed = write_env(h.env_dir, versions=VERSIONS, deployer=OLD_SP)
    h.fail_verify = {"switch"}
    assert h.apply() == 1
    assert h.journal()["transition"]["restored"]
    _seed_access(h, OLD_SP)
    _delete_principals(h)
    fixed = write_env(h.env_dir, versions=VERSIONS, deployer=NEW_SP)
    _promoted(h, old, failed, fixed)


def rebound(h, tmp_path: Path, monkeypatch) -> Path:
    """The AWS shape: a rolled-back change rebound by make policy-rebind, table SELECT kept, gate on its render."""
    _before_rebind(h)
    # The live refresh behind both renders: the restored (previous) generation's tags.
    out = pg.write_stage(h.env_dir / "data_access", _abandon_render(h), tmp_path / "refreshed")
    _refresh(h.env_dir, out / "abac.auto.tfvars")
    runner = _gated(h, tmp_path, monkeypatch)
    monkeypatch.setenv("GENIERAILS_COVERAGE_GATE_RUNNER", str(runner))  # as make policy-rebind sets it
    assert h.controller().rebind() == 0
    gate = json.loads((h.env_dir / "data_access" / cg.GATE_FILENAME).read_text())
    assert gate["status"] == "pass"
    h.rebind_fingerprint = gate["fingerprint"]
    assert sorted(f"table_access|{k}" for k in _table_grants(h)) == sorted(
        g for g in GRANTS if g.startswith("table_access|"))
    h.calls.clear()
    return runner


def _table_grants(h) -> list[str]:
    return [i["index_key"] for r in live_state(h.env_dir)["resources"]
            if (r["type"], r["name"]) == ("databricks_grant", "table_access") for i in r["instances"]]


def _make(target: str, h, tmp_path: Path, runner: Path, **stubs: str) -> subprocess.CompletedProcess:
    controller, derive = tmp_path / "controller.py", tmp_path / "derive.py"
    controller.write_text(CONTROLLER.format(shared=str(SHARED)))
    derive.write_text(DERIVE.format(shared=str(SHARED), ddl=DDL))
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
        ["make", "--no-print-directory", target, "ENV=dev", f"ENV_DIR={h.env_dir}", f"ROOT_RUNNER={runner}",
         # Next to the test's env, so _prepare-env never links the checkout's envs/account to it.
         f"ACCOUNT_ENV_DIR={h.env_dir.parent / 'account'}"],
        cwd=ROOT / "aws", text=True, capture_output=True, env=environ, timeout=600)


# make policy-rebind's preflight checks, which read live identities; not what these tests cover.
REBIND_CHECKS = {"deployer_identity.py check": "true", "group_map.py check": "true"}


def _make_abandon(h, tmp_path: Path, runner: Path) -> subprocess.CompletedProcess:
    return _make("policy-abandon", h, tmp_path, runner)


def _derives(tmp_path: Path) -> list[dict]:
    log = tmp_path / "derive.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.is_file() else []


# -- the AWS shape -------------------------------------------------------------------------
def test_make_policy_abandon_after_a_rebind_with_table_grants_rechecks_the_abandon_render(harness, tmp_path,
                                                                                          monkeypatch):
    runner = rebound(harness, tmp_path, monkeypatch)
    t = harness.journal()["transition"]
    old = pg.load_snapshot(harness.env_dir, t["from"])
    kept = _table_grants(harness)

    result = _make_abandon(harness, tmp_path, runner)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "Coverage check stale" not in out and f"Abandoned generation {t['to']}" in out
    # The check judged the abandon render (the restored generation), not the rebind render.
    gate = json.loads((harness.env_dir / "data_access" / cg.GATE_FILENAME).read_text())
    assert gate["status"] == "pass" and gate["fingerprint"] != harness.rebind_fingerprint
    rendered = (pg.stage_config_dir(harness.env_dir) / "abac.auto.tfvars").read_text()
    assert sorted(re.findall(r'^\s*name\s+=\s+"([^"]+)"', rendered, re.MULTILINE)) == sorted(
        p["name"] for p in old["fgac_policies"])
    state = live_state(harness.env_dir)
    assert {p["name"] for p in live_policies(state)} == {p["name"] for p in old["fgac_policies"]}
    assert _table_grants(harness) == kept
    assert harness.journal()["transition"] is None and harness.journal()["active"] == t["from"]


def test_abandon_of_a_change_that_never_switched_with_table_grants_rechecks_its_render(harness, tmp_path,
                                                                                       monkeypatch):
    """#186's other path: the last check (if any) judged another render, never the abandon one."""
    old = released(harness, deployer=OLD_SP)
    _seed_access(harness, OLD_SP)
    failed = write_env(harness.env_dir, versions=VERSIONS, deployer=OLD_SP)
    harness.fail_verify = {"create"}
    assert harness.apply() == 1
    _promoted(harness, old, failed)
    assert not harness.journal()["transition"]["switch_attempted"]
    out = pg.write_stage(harness.env_dir / "data_access", _abandon_render(harness), tmp_path / "refreshed")
    _refresh(harness.env_dir, out / "abac.auto.tfvars")
    runner = _gated(harness, tmp_path, monkeypatch)
    harness.calls.clear()

    result = _make_abandon(harness, tmp_path, runner)
    assert result.returncode == 0, result.stdout + result.stderr
    assert {p["name"] for p in live_policies(live_state(harness.env_dir))} == {p["name"] for p in old["fgac_policies"]}
    assert _table_grants(harness) and harness.journal()["transition"] is None


# -- make re-reads live UC first --------------------------------------------------------------
def test_make_policy_abandon_with_no_live_refresh_recorded_refreshes_it_under_the_lock(harness, tmp_path,
                                                                                      monkeypatch):
    """The live AWS shape after #196: no generated/.live_refresh.json (an earlier step invalidated it).

    The abandon hook's coverage check refused with "no live refresh of tags/DDL recorded ... re-run the
    same make command", and re-running didn't help: make policy-abandon never refreshed. It now does,
    under the env lock, before the controller plans.
    """
    runner = rebound(harness, tmp_path, monkeypatch)
    t = harness.journal()["transition"]
    (harness.env_dir / cg.REFRESH_RELPATH).unlink()

    result = _make_abandon(harness, tmp_path, runner)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "no live refresh of tags/DDL recorded" not in out and f"Abandoned generation {t['to']}" in out
    derives = _derives(tmp_path)
    assert len(derives) == 1 and derives[0]["locked"], derives
    assert out.index("GenieRails environment lock acquired (dev, policy-abandon)") < out.index(
        "=== Refresh Live DDL (dev) ===") < out.index("abandoning run")
    assert harness.journal()["transition"] is None and harness.journal()["active"] == t["from"]


def test_make_policy_abandon_stops_before_the_controller_when_the_live_refresh_fails(harness, tmp_path,
                                                                                   monkeypatch):
    runner = rebound(harness, tmp_path, monkeypatch)
    before = (harness.journal(), live_state(harness.env_dir))
    result = _make("policy-abandon", harness, tmp_path, runner, **{"derive_assignments.py": "false"})
    assert result.returncode != 0
    assert "abandoning run" not in result.stdout + result.stderr
    assert harness.calls == [] and (harness.journal(), live_state(harness.env_dir)) == before


def test_make_policy_rebind_with_no_live_refresh_recorded_refreshes_it_under_the_lock(harness, tmp_path,
                                                                                    monkeypatch):
    """policy-rebind's access phase runs the same coverage check, so it had the same gap."""
    _before_rebind(harness)
    (harness.env_dir / cg.REFRESH_RELPATH).unlink(missing_ok=True)
    runner = _gated(harness, tmp_path, monkeypatch)
    harness.calls.clear()

    result = _make("policy-rebind", harness, tmp_path, runner, **REBIND_CHECKS)
    out = result.stdout + result.stderr
    assert "no live refresh of tags/DDL recorded" not in out, out
    assert result.returncode == 0, out
    derives = _derives(tmp_path)
    assert len(derives) == 1 and derives[0]["locked"], derives
    assert out.index("GenieRails environment lock acquired (dev, policy-rebind)") < out.index(
        "=== Refresh Live DDL (dev) ===") < out.index("=== Policy rebind (dev)")
    assert harness.journal()["transition"]["rebound"]


def test_make_policy_rebind_stops_before_the_controller_when_the_live_refresh_fails(harness, tmp_path,
                                                                                  monkeypatch):
    _before_rebind(harness)
    runner = _gated(harness, tmp_path, monkeypatch)
    harness.calls.clear()
    before = (harness.journal(), live_state(harness.env_dir))
    result = _make("policy-rebind", harness, tmp_path, runner, **REBIND_CHECKS, **{"derive_assignments.py": "false"})
    assert result.returncode != 0
    assert "=== Policy rebind (dev)" not in result.stdout + result.stderr
    assert harness.calls == [] and (harness.journal(), live_state(harness.env_dir)) == before
    assert not harness.journal()["transition"].get("rebound")


# -- the check is not weakened ----------------------------------------------------------------
def _refused(h, capfd, match: str) -> str:
    before = (h.journal(), live_state(h.env_dir))
    capfd.readouterr()
    with pytest.raises(spa.Refused, match="abandon plan was refused"):
        h.controller().abandon()
    captured = capfd.readouterr()
    out = captured.out + captured.err
    assert re.search(match, out), out
    # Nothing was applied: the change is still recorded and Unity Catalog is as rebind left it.
    assert (h.journal(), live_state(h.env_dir)) == before
    assert h.calls == ["abandon:plan"]
    return out


def test_an_abandon_render_leaving_a_classified_column_unmasked_is_refused(harness, tmp_path, monkeypatch, capfd):
    rebound(harness, tmp_path, monkeypatch)
    t = harness.journal()["transition"]
    # The restored generation's snapshot lost the full mask of the treatment the column carries.
    old = pg.load_snapshot(harness.env_dir, t["from"])
    value = next(iter(old["tags"].values()))
    old["fgac_policies"] = [p for p in old["fgac_policies"] if p["name"] != f"gr_mask_cat_{value}_full"]
    pg.save_snapshot(harness.env_dir, old)
    assert not pg.stage_problems("abandon", _abandon_render(harness), pg.load_snapshot(harness.env_dir, t["to"]), old)

    _refused(harness, capfd, r"COVERAGE CHECK — classified but unprotected columns(.|\n)*cat\.sch\.t\.email")
    gate = json.loads((harness.env_dir / "data_access" / cg.GATE_FILENAME).read_text())
    assert gate["status"] == "fail"


def test_table_grants_changed_after_the_check_are_still_refused_as_stale(harness, tmp_path, monkeypatch, capfd):
    rebound(harness, tmp_path, monkeypatch)
    real = pg._rerun_coverage_gate

    def then_a_new_reader(*args, **kwargs):
        real(*args, **kwargs)
        access = json.loads(access_file(harness).read_text())
        access["grants"].append("table_access|cat.sch.t|analysts")
        access_file(harness).write_text(json.dumps(access))

    monkeypatch.setattr(pg, "_rerun_coverage_gate", then_a_new_reader)
    _refused(harness, capfd, r"Coverage check stale")
    assert json.loads((harness.env_dir / "data_access" / cg.GATE_FILENAME).read_text())["status"] == "pass"


def test_tags_that_differ_from_the_live_refresh_are_still_refused(harness, tmp_path, monkeypatch, capfd):
    rebound(harness, tmp_path, monkeypatch)
    # The last live refresh read other treatment tags than the abandon render leaves.
    record = json.loads((harness.env_dir / cg.REFRESH_RELPATH).read_text())
    (harness.env_dir / cg.REFRESH_RELPATH).write_text(json.dumps(record | {"tag_assignments_sha256": "0" * 64}))
    _refused(harness, capfd, r"coverage check stopped the stage")
    assert json.loads((harness.env_dir / "data_access" / cg.GATE_FILENAME).read_text())["status"] == "fail"


def test_every_make_entry_to_the_staged_controller_that_plans_passes_the_coverage_runner():
    makefile = (SHARED / "Makefile.shared").read_text().replace("\\\n", " ")
    calls = re.findall(r"^.*\$\(_STAGED_POLICY\) (run|plan|apply-saved|rebind|abandon)\b.*$", makefile, re.MULTILINE)
    assert set(calls) == {"run", "plan", "apply-saved", "rebind", "abandon"}
    for line in re.findall(r"^.*\$\(_STAGED_POLICY\) (?:run|plan|apply-saved|rebind|abandon)\b.*$", makefile,
                           re.MULTILINE):
        assert 'GENIERAILS_COVERAGE_GATE_RUNNER="$(ROOT_RUNNER)" $(_STAGED_POLICY)' in line, line
