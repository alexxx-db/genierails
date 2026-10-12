from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

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


def test_snapshot_diff_is_stable_and_order_insensitive():
    before = {"grants": [{"p": "b"}, {"p": "a"}], "tags": ["x"], "serial": 4}
    after = {"grants": [{"p": "a"}, {"p": "b"}], "tags": ["y"], "serial": 5}
    assert driver.snapshot_diff(before, after) == {
        "serial": {"before": 4, "after": 5},
        "tags": {"before": ["x"], "after": ["y"]},
    }


def test_missing_target_has_step_marker():
    s1 = next(s for s in driver.SCENARIOS if s.key == "s1")
    plan = driver.scenario_plan(s1, {"check", "rehearse", "promote-to", "release", "maintain"})
    capture = next(item for item in plan if item["command"].startswith("make capture"))
    assert capture["status"] == "skip"
    assert capture["skip_reason"] == "requires step 11"


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
        "status": "passed", "steps": [], "snapshot_diff": {}, "client_token": "never-write-me"
    })
    assert "never-write-me" not in (tmp_path / "champion.json").read_text()


def test_scenario_catalog_contains_plan_matrix_and_governance_only():
    keys = {s.key for s in driver.SCENARIOS}
    assert {f"s{i}" for i in range(1, 17)} <= keys
    assert {"champion", "manifest", "migration"} <= keys
    assert next(s for s in driver.SCENARIOS if s.key == "s14").title == "Governance-only environment"
