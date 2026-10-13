from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from types import SimpleNamespace
import types

import pytest

from scripts.e2e import driver


ROOT = Path(__file__).resolve().parents[2]


def test_dry_run_is_offline_and_writes_reports(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run must not invoke subprocess or snapshots")
    monkeypatch.setattr(subprocess, "run", forbidden)
    rc = driver.main(["--cloud", "aws", "--repo", str(ROOT), "--dry-run",
                      "--scenarios", "champion,s1", "--reports", str(tmp_path)])
    assert rc == 0
    champion = json.loads((tmp_path / "champion.json").read_text())
    assert champion["status"] == "dry-run"
    assert any(item["command"] == "make setup ENV=dev" for item in champion["steps"])
    assert not any("terraform" in item["command"].split()[:1] for item in champion["steps"])
    assert (tmp_path / "s1.md").is_file()
    assert json.loads((tmp_path / "s1.json").read_text())["would_not_run"] == []


def test_snapshot_diff_is_stable_and_order_insensitive():
    before = {"grants": [{"p": "b"}, {"p": "a"}], "tags": ["x"], "serial": 4}
    after = {"grants": [{"p": "a"}, {"p": "b"}], "tags": ["y"], "serial": 5}
    assert driver.snapshot_diff(before, after) == {
        "serial": {"before": 4, "after": 5},
        "tags": {"before": ["x"], "after": ["y"]},
    }


def test_missing_target_has_step_marker():
    s1 = next(s for s in driver.SCENARIOS if s.key == "s1")
    fake = dataclasses.replace(s1.steps[1], command=("make", "future-target", "ENV=dev"),
                               target="future-target", requires_step=99)
    plan = driver.scenario_plan(dataclasses.replace(s1, steps=(s1.steps[0], fake, *s1.steps[2:])), REAL_TARGETS)
    item = next(item for item in plan if item["command"].startswith("make future-target"))
    assert item["status"] == "skip"
    assert item["skip_reason"] == "requires step 99"


def test_every_executable_step_uses_public_make_target():
    for scenario in driver.SCENARIOS:
        for step in scenario.steps:
            if step.command:
                assert step.command[0] == "make"
                assert "terraform" not in step.command


def test_secret_fields_are_redacted():
    assert driver.redact({"client_secret": "nope", "nested": {"token": "bad", "ok": 1}}) == {
        "client_secret": "<redacted>", "nested": {"token": "<redacted>", "ok": 1}
    }


def test_report_redacts_secrets(tmp_path):
    scenario = driver.SCENARIOS[0]
    driver.write_report(tmp_path, scenario, {
        "status": "passed", "steps": [], "snapshot_diff": {}, "client_token": "never-write-me",
        "error": "failed with plain-secret-value inside",
    }, secrets={"plain-secret-value"})
    text = (tmp_path / "champion.json").read_text() + (tmp_path / "champion.md").read_text()
    assert "never-write-me" not in text and "plain-secret-value" not in text


def test_scenario_catalog_contains_plan_matrix_and_governance_only():
    keys = {s.key for s in driver.SCENARIOS}
    assert {f"s{i}" for i in range(1, 17)} <= keys
    assert {"champion", "manifest", "migration"} <= keys
    assert next(s for s in driver.SCENARIOS if s.key == "s14").title == "Governance-only environment"


def test_preconditions_are_not_split_into_characters():
    assert all(all(len(item) > 1 for item in scenario.preconditions) for scenario in driver.SCENARIOS)


# --- live loop with fake git/make/snapshots -------------------------------------------

COMMIT = "a" * 40
OTHER = "b" * 40
ALL_TARGETS = {step.target for scenario in driver.SCENARIOS for step in scenario.steps if step.target}
REAL_TARGETS = set(re.findall(r"^([a-zA-Z0-9][\w.-]*):(?:\s|$)",
                              (ROOT / "shared" / "Makefile.shared").read_text(), re.M))


def snapshot(masks=("mask_ssn",), tags=(("t", "ssn", "class.ssn"),), grants=(), policy_except=()):
    return {"uc": {"cat": {
        "governed_tags": [{"catalog_name": "cat", "schema_name": "s", "table_name": t, "column_name": c,
                           "tag_name": "pii", "tag_value": v} for t, c, v in tags],
        "mask_functions": [{"routine_catalog": "cat", "routine_schema": "s", "routine_name": m} for m in masks],
        "abac_policies": [{"table": "cat.s.t", "policies": [{"name": "mask_ssn_p", "to_principals": ["account users"],
                           "except_principals": list(policy_except), "column_mask": {"function_name": "cat.s.mask_ssn"}}]}],
        "table_grants": [{"grantee": g, "privilege_type": "SELECT", "table_catalog": "cat", "table_schema": "s",
                          "table_name": "t"} for g in grants],
    }}, "agents": []}


class FakeSnapshots:
    mode = "steady"

    def __init__(self, *_):
        self.n = 0

    def capture(self, env):
        self.n += 1
        if self.mode == "boom" and self.n == 3:
            raise RuntimeError("snapshot exploded")
        if self.mode == "weaken" and env == "prod" and self.n > 2:
            return snapshot(masks=())
        if self.mode == "widen_after" and env == "prod" and self.n > 2:
            return snapshot(grants=("stranger",))
        return snapshot()


def live_args(tmp_path, scenario, *extra):
    checkout = tmp_path / "checkout"
    (checkout / "aws").mkdir(parents=True, exist_ok=True)
    return ["--cloud", "aws", "--repo", str(checkout), "--checkout", str(checkout),
            "--commit", COMMIT, "--state-dir", str(tmp_path / "state"),
            "--reports", str(tmp_path / "reports"), "--scenarios", scenario,
            "--confirm-live", *extra]


def fake_subprocess(calls, *, unrelated=False, no_manifest=False, secrets=False, refuse=None,
                    drift_after_make=False, on_make=None):
    state = {"makes": 0}

    def run(argv, **kwargs):
        argv = list(argv); calls.append((argv, kwargs.get("env", {})))
        if argv[:2] == ["git", "rev-parse"]:
            head = OTHER if drift_after_make and state["makes"] and argv[2:] == ["HEAD"] else COMMIT
            return subprocess.CompletedProcess(argv, 0, head + "\n", "")
        if argv[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(argv, 2, "error: No such remote\n", "")
        if argv and argv[0] == "git":
            return subprocess.CompletedProcess(argv, 0, "", "")
        text, rc = "ok\n", 0
        if argv and argv[0] == "make":
            state["makes"] += 1
            if on_make: on_make(argv)
            if "release-plan" in argv:
                text = "ok\n" if no_manifest else "  Manifest (immutable): /tmp/approved.json\n"
            if "ungovern" in argv and unrelated:
                rc, text = 2, "unrelated syntax error\n"
            if refuse and refuse[0](argv):
                rc, text = 2, refuse[1]
            if secrets:
                text += ('payload {"token": "dapiSECRET123"}\nAuthorization: Bearer dapiSECRET456\n'
                         "client secret is file-secret-value\n")
        return subprocess.CompletedProcess(argv, rc, text, "")
    return run


def makes(calls):
    return [argv for argv, _ in calls if argv and argv[0] == "make"]


@pytest.fixture
def live(monkeypatch):
    calls: list = []

    def setup(targets=ALL_TARGETS, **fake):
        monkeypatch.setattr(driver, "make_targets", lambda *_: set(targets))
        monkeypatch.setattr(driver.subprocess, "run", fake_subprocess(calls, **fake))
        return calls
    return setup


# Blocker 1: a scenario needing a genuinely unbuilt target runs nothing at all.
def test_missing_fake_target_scenario_is_not_run_and_issues_no_make(tmp_path, live, monkeypatch):
    s1 = next(s for s in driver.SCENARIOS if s.key == "s1")
    fake = dataclasses.replace(s1.steps[1], command=("make", "future-target", "ENV=dev"),
                               target="future-target", requires_step=99)
    monkeypatch.setattr(driver, "SCENARIOS", (dataclasses.replace(s1, steps=(s1.steps[0], fake, *s1.steps[2:])),))
    calls = live(REAL_TARGETS)
    assert driver.main(live_args(tmp_path, "s1"), snapshot_factory=FakeSnapshots) == 0
    report = json.loads((tmp_path / "reports" / "s1.json").read_text())
    assert report["status"] == "NOT RUN"
    assert all(item["status"] in ("NOT RUN", "skip") for item in report["steps"])
    assert not makes(calls)


def test_step_10_11_scenarios_are_runnable_in_this_repo_and_dry_run_is_offline(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run must not invoke subprocess or snapshots")
    monkeypatch.setattr(subprocess, "run", forbidden)
    keys = "s1,s3,s7,s13,migration"
    assert driver.main(["--cloud", "aws", "--repo", str(ROOT), "--dry-run",
                        "--scenarios", keys, "--reports", str(tmp_path)]) == 0
    for key in keys.split(","):
        report = json.loads((tmp_path / f"{key}.json").read_text())
        assert report["status"] == "dry-run"
        assert report["would_not_run"] == []


# Blocker 2: refusals, placeholders, CI and unrelated failures.
def test_unresolved_placeholder_is_error_without_make(tmp_path, live):
    calls = live()
    assert driver.main(live_args(tmp_path, "manifest"), snapshot_factory=FakeSnapshots) == 1
    report = json.loads((tmp_path / "reports" / "manifest.json").read_text())
    assert report["status"] == "ERROR"
    assert "unresolved placeholder" in report["error"]
    assert not makes(calls)


def test_late_placeholder_is_caught_before_any_step_runs(tmp_path, live):
    calls = live()
    assert driver.main(live_args(tmp_path, "s5"), snapshot_factory=FakeSnapshots) == 1
    assert "treatment" in json.loads((tmp_path / "reports" / "s5.json").read_text())["error"]
    assert not makes(calls)


@pytest.mark.parametrize("arg", ["MANIFEST=", "MANIFEST=<from make release-plan>", "TABLE={table}"])
def test_placeholder_or_empty_argument_never_reaches_make(arg):
    with pytest.raises(ValueError, match="unresolved placeholder"):
        driver.check_argument(arg)
    with pytest.raises(ValueError):
        driver.render(arg, {"table": ""})


def test_unrelated_nonzero_is_not_a_passing_refusal(tmp_path, live):
    live(unrelated=True)
    assert driver.main(live_args(tmp_path, "s8", "--value", "table=c.s.t", "--value", "uncovered_table=c.s.u"),
                       snapshot_factory=FakeSnapshots) == 1
    report = json.loads((tmp_path / "reports" / "s8.json").read_text())
    assert report["status"] == "ERROR"
    assert report["steps"][0]["status"] == "ERROR"


def test_refusal_message_with_changed_snapshot_is_an_error(tmp_path, live):
    class Mutates(FakeSnapshots):
        def capture(self, env):
            self.n += 1
            return snapshot(grants=("x",) if self.n > 4 else ())
    live(refuse=(lambda argv: "release-plan" in argv,
                 "release-plan: refused by the reader or weakening check (above)\n"))
    assert driver.main(live_args(tmp_path, "s6", "--value", "ack_entry=prod:reader:c.s.t:g"),
                       snapshot_factory=Mutates) == 1
    report = json.loads((tmp_path / "reports" / "s6.json").read_text())
    assert report["steps"][0]["assertion"] == "refusal changed the snapshot"


def test_s13_sets_ci_unsets_override_and_passes_on_exact_refusals(tmp_path, live):
    calls = live(refuse=(lambda argv: True, "ERROR: applying targets cannot run in CI. ...\n"))
    assert driver.main(live_args(tmp_path, "s13", "--value", "agent_a=A"), snapshot_factory=FakeSnapshots) == 0
    envs = [env for argv, env in calls if argv and argv[0] == "make"]
    assert len(envs) == 6 and all(env.get("CI") == "true" for env in envs)
    assert all("GENIERAILS_ALLOW_CI_APPLY" not in env for env in envs)
    assert all("<" not in " ".join(argv) for argv in makes(calls))
    report = json.loads((tmp_path / "reports" / "s13.json").read_text())
    assert report["status"] == "passed" and report["restored_lifecycle"] is False


def test_missing_release_plan_manifest_errors_before_release(tmp_path, live):
    calls = live(no_manifest=True)
    assert driver.main(live_args(tmp_path, "s14"), snapshot_factory=FakeSnapshots) == 1
    scenario_makes = makes(calls)
    assert any("release-plan" in argv for argv in scenario_makes)
    assert not any("release" in argv for argv in scenario_makes)


# Blocker 3: snapshots read this repo's layout and pin the env's host.
def _fake_sdk(monkeypatch, seen, *, resolved_host=None, tables=(("prodcat", "s", "t"),)):
    class Statement:
        def execute_statement(self, *, statement, **kwargs):
            rows = [list(t) for t in tables] if "information_schema.tables" in statement else []
            cols = ["table_catalog", "table_schema", "table_name"]
            return SimpleNamespace(status=SimpleNamespace(state="SUCCEEDED"), statement_id="s",
                manifest=SimpleNamespace(schema=SimpleNamespace(columns=[SimpleNamespace(name=c) for c in cols])),
                result=SimpleNamespace(data_array=rows, next_chunk_index=None))
    class Client:
        def __init__(self, **kwargs):
            seen.update(kwargs); seen["ambient"] = os.environ.get("DATABRICKS_HOST")
            self.config = SimpleNamespace(host=resolved_host or kwargs["host"])
            self.statement_execution = Statement()
            self.policies = SimpleNamespace(list_policies=lambda *a, **k: [])
            self.genie = SimpleNamespace(); self.permissions = SimpleNamespace()
    package = types.ModuleType("databricks"); sdk = types.ModuleType("databricks.sdk"); sdk.WorkspaceClient = Client
    monkeypatch.setitem(sys.modules, "databricks", package); monkeypatch.setitem(sys.modules, "databricks.sdk", sdk)


def _env_files(tmp_path):
    cloud = tmp_path / "aws"; env = cloud / "envs" / "prod"; env.mkdir(parents=True)
    (env / "auth.auto.tfvars").write_text('databricks_workspace_host = "https://pinned.example"\n'
        'databricks_client_id = "id"\ndatabricks_client_secret = "actual-secret"\n')
    (env / "env.auto.tfvars").write_text('sql_warehouse_id = "wh"\nuc_tables = ["prodcat.s.t"]\n'
        'catalog_map = { devcat = "prodcat" }\ngenie_spaces = []\n')
    return cloud


def test_live_snapshot_uses_env_auth_file_and_never_the_shell(tmp_path, monkeypatch):
    pytest.importorskip("hcl2")
    cloud = _env_files(tmp_path); seen = {}
    monkeypatch.setenv("DATABRICKS_HOST", "https://shell-default.example")
    _fake_sdk(monkeypatch, seen)
    provider = driver.LiveSnapshotProvider(cloud, tmp_path / "state")
    snap = provider.capture("prod")
    assert seen["host"] == "https://pinned.example" and seen["auth_type"] == "oauth-m2m"
    assert seen["ambient"] is None and os.environ["DATABRICKS_HOST"] == "https://shell-default.example"
    assert "profile" not in seen
    assert "actual-secret" in provider.secret_values
    assert snap["uc"]["prodcat"]["abac_policies"] == [{"table": "prodcat.s.t", "policies": []}]


def test_live_snapshot_refuses_a_client_resolved_to_another_host(tmp_path, monkeypatch):
    cloud = _env_files(tmp_path)
    _fake_sdk(monkeypatch, {}, resolved_host="https://elsewhere.example")
    with pytest.raises(RuntimeError, match="not the env's"):
        driver.LiveSnapshotProvider(cloud, tmp_path / "state").client("prod")


def test_live_snapshot_read_failures_are_errors_not_data(tmp_path, monkeypatch):
    pytest.importorskip("hcl2")
    cloud = _env_files(tmp_path)
    _fake_sdk(monkeypatch, {}, tables=())
    with pytest.raises(RuntimeError, match="no visible tables"):
        driver.LiveSnapshotProvider(cloud, tmp_path / "state").capture("prod")
    response = SimpleNamespace(status=SimpleNamespace(state="FAILED"), result=None, manifest=None)
    with pytest.raises(RuntimeError, match="did not succeed"):
        driver._require_succeeded(response, "tags")
    state = tmp_path / "state"; state.mkdir(); (state / "x.tfstate").write_text("{not json")
    with pytest.raises(RuntimeError, match="Terraform serial"):
        driver._state_serials(state)
    with pytest.raises(RuntimeError, match="missing authentication file"):
        driver.LiveSnapshotProvider(cloud, state).client("dev")


def test_agent_ids_include_hcl_state_and_id_file(tmp_path):
    env = tmp_path / "env"; env.mkdir(); state = tmp_path / "state"; state.mkdir()
    (env / ".genie_space_id").write_text("file-id\n")
    (state / "workspace.tfstate").write_text(json.dumps({"resources": [{"type": "databricks_genie_space",
        "name": "state_agent", "instances": [{"attributes": {"id": "state-id"}}]}]}))
    ids = driver._agent_ids(env, {"genie_spaces": [{"name": "HCL", "genie_space_id": "hcl-id"}]}, state, tmp_path)
    assert {value for _, value in ids} == {"file-id", "state-id", "hcl-id"}


# Blocker 4: restore after every scenario, on errors and signals; weaker = failure.
def test_exception_writes_report_and_attempts_public_restore(tmp_path, live):
    class Boom(FakeSnapshots): mode = "boom"
    calls = live()
    assert driver.main(live_args(tmp_path, "s4"), snapshot_factory=Boom) == 1
    report = json.loads((tmp_path / "reports" / "s4.json").read_text())
    assert report["status"] in ("ERROR", "RESTORE FAILED")
    assert [a[1] for a in makes(calls)][-4:] == ["rehearse", "release-plan", "release", "maintain"]


def test_sigterm_mid_step_restores_writes_partial_report_and_stops(tmp_path, live):
    fired = []
    def on_make(argv):
        if "maintain" in argv and not fired:
            fired.append(1); os.kill(os.getpid(), signal.SIGTERM)
    calls = live(on_make=on_make)
    assert driver.main(live_args(tmp_path, "s4,s10"), snapshot_factory=FakeSnapshots) == 1
    report = json.loads((tmp_path / "reports" / "s4.json").read_text())
    assert report["status"] == "interrupted" and "SIGTERM" in report["error"]
    assert report["restored_lifecycle"] is True
    assert [a[1] for a in makes(calls)] == ["maintain", "rehearse", "release-plan", "release", "maintain"]
    assert not (tmp_path / "reports" / "s10.json").exists()
    assert signal.getsignal(signal.SIGTERM) is not driver._raise_interrupt


def test_weaker_protection_fails_scenario_and_final_check(tmp_path, live):
    class Weaken(FakeSnapshots): mode = "weaken"
    live()
    assert driver.main(live_args(tmp_path, "s4"), snapshot_factory=Weaken) == 1
    assert json.loads((tmp_path / "reports" / "s4.json").read_text())["status"] == "RESTORE FAILED"
    final = json.loads((tmp_path / "reports" / "final-protection.json").read_text())
    assert final["status"] == "ERROR" and final["weaker_than_baseline"]["prod"]["masks"] == ["cat.s.mask_ssn"]


def test_protection_diff_is_per_column_and_principal():
    base = snapshot()
    assert driver.protection_diff(base, snapshot()) == {}
    assert driver.protection_diff(base, snapshot(tags=()))["columns"] == ["cat.s.t.ssn pii=class.ssn"]
    assert driver.protection_diff(base, snapshot(policy_except=("analysts",)))["policies"] == [
        "cat.s.t mask_ssn_p: newly exempts analysts"]
    assert driver.protection_diff(base, snapshot(grants=("eve",)), include_grants=True)["widened_grants"] == [
        "eve SELECT cat.s.t"]


def test_widened_grant_after_restore_fails(tmp_path, live):
    class Widen(FakeSnapshots): mode = "widen_after"
    live()
    assert driver.main(live_args(tmp_path, "s4"), snapshot_factory=Widen) == 1
    assert json.loads((tmp_path / "reports" / "s4.json").read_text())["status"] == "RESTORE FAILED"


# Blocker 6: human gates.
def test_human_scenario_without_tty_is_not_run(tmp_path, live):
    calls = live()
    assert driver.main(live_args(tmp_path, "champion"), snapshot_factory=FakeSnapshots) == 0
    report = json.loads((tmp_path / "reports" / "champion.json").read_text())
    assert report["status"] == "NOT RUN"
    assert not makes(calls)


def test_closed_stdin_at_gate_is_not_run_and_aborts_the_run(tmp_path, live, monkeypatch):
    calls = live()
    monkeypatch.setattr(driver.sys.stdin, "isatty", lambda: True)
    def eof(_): raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    driver.main(live_args(tmp_path, "s16,manifest"), snapshot_factory=FakeSnapshots)
    report = json.loads((tmp_path / "reports" / "s16.json").read_text())
    assert report["status"] == "NOT RUN" and "stdin closed" in report["error"]
    assert report["steps"][1]["status"] == "planned"
    assert not makes(calls) and not (tmp_path / "reports" / "manifest.json").exists()


def test_wrong_human_confirmation_is_error_and_reported(tmp_path, live, monkeypatch):
    live()
    monkeypatch.setattr(driver.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "not-champion")
    assert driver.main(live_args(tmp_path, "champion"), snapshot_factory=FakeSnapshots) == 1
    report = json.loads((tmp_path / "reports" / "champion.json").read_text())
    assert report["status"] == "ERROR"
    assert "exact token" in report["error"]


# Blocker 7: redaction.
def test_logs_redact_json_bearer_token_and_auth_file_values(tmp_path, live):
    live(secrets=True)
    env = tmp_path / "checkout" / "aws" / "envs" / "dev"; env.mkdir(parents=True)
    (env / "auth.auto.tfvars").write_text('databricks_client_secret = "file-secret-value"\n')
    driver.main(live_args(tmp_path, "s4"), snapshot_factory=FakeSnapshots)
    logs = "\n".join(path.read_text() for path in (tmp_path / "reports" / "logs").glob("*.log"))
    assert "maintain" in logs
    assert "dapiSECRET" not in logs and "file-secret-value" not in logs
    assert "Authorization: Bearer" not in logs


def test_redact_text_covers_env_assignments_and_bearer():
    text = driver.redact_text("DATABRICKS_TOKEN=abc123 curl -H 'Authorization: Bearer xyz' x Bearer abcdefghijkl")
    assert "abc123" not in text and "xyz" not in text and "abcdefghijkl" not in text


# Blocker 8: the checkout is pinned to the requested commit.
def test_head_drift_mid_scenario_is_an_error_before_the_next_step(tmp_path, live):
    calls = live(drift_after_make=True)
    assert driver.main(live_args(tmp_path, "s4"), snapshot_factory=FakeSnapshots) == 1
    report = json.loads((tmp_path / "reports" / "s4.json").read_text())
    assert "not the pinned" in report["error"]
    assert "exit_code" not in report["steps"][1]
    assert makes(calls) == [["make", "maintain", "ENV=prod"]]


def test_every_scenario_starts_by_pinning_the_requested_commit(tmp_path, live):
    calls = live()
    assert driver.main(live_args(tmp_path, "s4,s14"), snapshot_factory=FakeSnapshots) == 0
    pins = [argv for argv, _ in calls if argv[:4] == ["git", "checkout", "--force", "--detach"]]
    first_make = next(i for i, (argv, _) in enumerate(calls) if argv[0] == "make")
    # The initial checkout and the scenario pin both precede the first command.
    assert sum(1 for argv, _ in calls[:first_make] if argv[:4] == ["git", "checkout", "--force", "--detach"]) == 2
    assert all(argv[4] == COMMIT for argv in pins)


# Blocker 5 and the reviewer's simulated live run, offline: real git, fake make.
FAKE_MAKE = r'''#!{python}
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
log = pathlib.Path(os.environ["FAKE_MAKE_LOG"])
if args[:1] == ["-qp"]:
    print("help:"); [print(t + ":") for t in json.loads(os.environ["FAKE_TARGETS"])]; sys.exit(1)
target = args[0]; kv = dict(a.split("=", 1) for a in args[1:] if "=" in a)
def git(*a): return subprocess.run(["git", *a], capture_output=True, text=True).stdout.strip()
head = git("rev-parse", "HEAD"); dirty = git("status", "--porcelain")
with log.open("a") as fh:
    fh.write(json.dumps({{"argv": args, "head": head, "dirty": bool(dirty),
                         "remote": os.environ.get("RELEASE_REMOTE")}}) + "\n")
state = pathlib.Path(os.environ["GENIERAILS_STATE_DIR"]); flag = state / "widening"
if target in ("rehearse", "release-plan", "release", "maintain") and dirty:
    print(f"{{target}}: the checkout is not clean"); sys.exit(1)
if target == "generate":
    p = pathlib.Path("envs/dev/generated/abac.auto.tfvars"); p.write_text(p.read_text() + "# regenerated\n")
elif target == "promote-to" and kv.get("CHECK") != "1":
    pathlib.Path("envs/prod/generated/abac.auto.tfvars").write_text(
        pathlib.Path("envs/dev/generated/abac.auto.tfvars").read_text())
elif target == "release-plan":
    subprocess.run(["git", "fetch", "--quiet", os.environ["RELEASE_REMOTE"], "main"], check=True)
    upstream = git("rev-parse", "FETCH_HEAD")
    if subprocess.run(["git", "merge-base", "--is-ancestor", head, upstream]).returncode:
        print(f"release-plan refused:\n  - HEAD {{head[:12]}} is not on {{os.environ['RELEASE_REMOTE']}}/main"); sys.exit(1)
    ack = pathlib.Path("envs/prod/ack.txt")
    if flag.exists() and "prod:reader:" not in (ack.read_text() if ack.exists() else ""):
        print("ERROR: unacknowledged reader prod:reader:cat.s.t:g: widens SELECT")
        print("release-plan: refused by the reader or weakening check (above); No manifest was written."); sys.exit(1)
    manifest = state / f"manifest-{{head}}.json"; manifest.write_text("{{}}")
    print(f"  Manifest (immutable): {{manifest}}")
elif target == "release":
    if not pathlib.Path(kv["MANIFEST"]).is_file(): print("release: no manifest"); sys.exit(1)
    flag.unlink(missing_ok=True)
print(f"{{target}} ok")
'''


@pytest.fixture
def sim(tmp_path, monkeypatch):
    # Hermetic git: no global hooks, signing or identity from the developer's machine.
    (tmp_path / "gitconfig").write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    checkout = tmp_path / "checkout"; cloud = checkout / "aws"
    for env in ("dev", "prod"):
        (cloud / "envs" / env / "generated").mkdir(parents=True)
        (cloud / "envs" / env / "generated" / "abac.auto.tfvars").write_text("# baseline\n")
    (cloud / "Makefile").write_text("include ../shared/Makefile.shared\n")
    git_env = {**os.environ, **driver.GIT_IDENTITY}
    subprocess.run(["git", "init", "-q", "-b", "main", str(checkout)], check=True, env=git_env)
    subprocess.run(["git", "-C", str(checkout), "add", "-A"], check=True, env=git_env)
    subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "baseline"], check=True, env=git_env)
    baseline = _git(checkout, "rev-parse", "HEAD")
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    (bin_dir / "make").write_text(FAKE_MAKE.format(python=sys.executable)); (bin_dir / "make").chmod(0o755)
    state = tmp_path / "state"; state.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_MAKE_LOG", str(tmp_path / "make.log"))
    monkeypatch.setenv("FAKE_TARGETS", json.dumps(sorted(REAL_TARGETS)))
    monkeypatch.delenv("CI", raising=False)
    args = ["--cloud", "aws", "--repo", str(checkout), "--checkout", str(checkout), "--commit", baseline,
            "--state-dir", str(state), "--reports", str(tmp_path / "reports"), "--confirm-live"]

    def log():
        path = tmp_path / "make.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return SimpleNamespace(checkout=checkout, baseline=baseline, args=args, log=log, state=state,
                           reports=tmp_path / "reports")


def _git(repo, *argv):
    return subprocess.run(["git", "-C", str(repo), *argv], capture_output=True, text=True, check=True).stdout.strip()


def test_simulated_live_s6_merges_ack_on_driver_branch_and_restores(sim):
    (sim.state / "widening").write_text("")
    rc = driver.main([*sim.args, "--scenarios", "s6", "--value", "ack_entry=prod:reader:cat.s.t:g"],
                     snapshot_factory=FakeSnapshots)
    report = json.loads((sim.reports / "s6.json").read_text())
    assert rc == 0, report["error"]
    assert report["status"] == "passed" and report["restored_lifecycle"] is True
    log = sim.log()
    targets = [entry["argv"][0] for entry in log]
    assert targets[:2] == ["release-plan", "check"] and targets[-4:] == ["rehearse", "release-plan", "release", "maintain"]
    assert all(entry["remote"] == driver.E2E_REMOTE for entry in log)
    applying = [e for e in log if e["argv"][0] in ("rehearse", "release-plan", "release", "maintain")]
    assert not any(e["dirty"] for e in applying)
    # The released commit is a merge on the driver's main that carries the ack.
    released = next(e for e in log if e["argv"][0] == "release")["head"]
    assert "prod:reader:cat.s.t:g" in _git(sim.checkout, "show", f"{released}:aws/envs/prod/ack.txt")
    assert len(_git(sim.checkout, "rev-list", "--parents", "-n1", released).split()) == 3
    assert _git(sim.checkout, "branch", "--list", f"{driver.E2E_BRANCH_PREFIX}s6-*")
    # Restored: checkout pinned and clean; the driver's main is a forward fix with the baseline tree.
    assert _git(sim.checkout, "rev-parse", "HEAD") == sim.baseline
    assert _git(sim.checkout, "status", "--porcelain") == ""
    tip = _git(sim.checkout, "rev-parse", f"{driver.E2E_BRANCH_PREFIX}main")
    assert tip != sim.baseline and _git(sim.checkout, "rev-parse", f"{tip}^{{tree}}") == _git(
        sim.checkout, "rev-parse", f"{sim.baseline}^{{tree}}")
    assert "origin" not in _git(sim.checkout, "remote").split()
    assert json.loads((sim.reports / "final-protection.json").read_text())["status"] == "passed"


def test_simulated_live_champion_commits_generate_then_promotion(sim, monkeypatch):
    monkeypatch.setattr(driver.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "champion")
    rc = driver.main([*sim.args, "--scenarios", "champion,s4"], snapshot_factory=FakeSnapshots)
    champion = json.loads((sim.reports / "champion.json").read_text())
    assert rc == 0, champion["error"]
    assert champion["status"] == "passed" and len(champion["confirmations"]) == 2
    log = sim.log()
    rehearses = [e for e in log if e["argv"][0] == "rehearse"]
    assert rehearses and not any(e["dirty"] for e in rehearses)
    assert len({e["head"] for e in rehearses[:2]}) == 2  # generate merge, then promotion merge
    assert any(e["argv"][:2] == ["maintain", "ENV=prod"] and e["head"] == sim.baseline for e in log)
    assert json.loads((sim.reports / "s4.json").read_text())["status"] == "passed"
    assert _git(sim.checkout, "rev-parse", "HEAD") == sim.baseline
