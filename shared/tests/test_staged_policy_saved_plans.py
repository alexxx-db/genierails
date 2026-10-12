"""Two-phase release through the staged policy controller (rollout step 9).

``plan`` saves the next phases' plans and applies nothing; ``apply-saved``
applies exactly those plans. Uses the real hooks against the fake Terraform
layer of test_staged_policy_apply (whose invariant checks run after every
hook): nobody is ever unmasked, nobody is doubly masked outside a tier move.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts import policy_generations as pg
from scripts import staged_policy_apply as spa
from tests.test_staged_policy_apply import harness, live_state, released, write_env  # noqa: F401

# A treatment change: a new generation, so create -> verify -> switch -> verify -> grant.
CHANGE = {"email_partial": {"partial": "redacted"}}


def _runner_log(h) -> list[str]:
    path = h.env_dir / "runner.log"
    return path.read_text().splitlines() if path.is_file() else []


def _plan(h, tmp_path, name="plan") -> tuple[Path, dict]:
    out = tmp_path / name
    assert h.controller().plan(out) == 0
    return out, json.loads((out / spa.PHASES_FILE).read_text())


def test_plan_applies_nothing_and_leaves_the_journal_alone(harness, tmp_path):
    released(harness)
    state, journal = live_state(harness.env_dir), harness.journal()
    write_env(harness.env_dir, versions=CHANGE)
    before = len(_runner_log(harness))
    _out, phases = _plan(harness, tmp_path)
    assert live_state(harness.env_dir) == state and harness.journal() == journal
    assert not [line for line in _runner_log(harness)[before:] if line.startswith("apply-plan")]
    assert phases["steps"][0]["phase"] == "create" and phases["steps"][0]["changes"] is True


def test_a_release_with_no_policy_change_is_one_manifest(harness, tmp_path):
    gen = released(harness)
    write_env(harness.env_dir)  # same generation
    out, phases = _plan(harness, tmp_path)
    assert phases["final"] is True
    assert [s["phase"] for s in phases["steps"]] == ["create", "grant"]
    assert harness.controller().apply_saved(out) == 0
    journal = harness.journal()
    assert journal["transition"] is None and journal["active"] == gen["fingerprint"]


def test_a_saved_plan_that_changes_nothing_is_not_applied(harness, tmp_path):
    released(harness)
    write_env(harness.env_dir)
    out, _phases = _plan(harness, tmp_path)
    before = len(_runner_log(harness))
    assert harness.controller().apply_saved(out) == 0
    assert not [line for line in _runner_log(harness)[before:] if line.startswith(("apply-plan", "plan"))]


def test_a_policy_change_takes_one_manifest_per_changing_phase(harness, tmp_path):
    first = released(harness)
    desired = write_env(harness.env_dir, versions=CHANGE)
    applied = []
    for round_ in range(6):
        out, phases = _plan(harness, tmp_path, f"plan{round_}")
        changing = [s for s in phases["steps"] if s["kind"] == "apply" and s["changes"]]
        assert len(changing) <= 1, phases  # one changing phase per manifest
        rc = harness.controller().apply_saved(out)
        applied += [s["phase"] for s in phases["steps"]]
        if phases["final"]:
            assert rc == 0
            break
        assert rc == spa.MORE_PHASES
        assert harness.journal()["transition"]["status"] == "waiting"
    else:
        pytest.fail("the change never finished")
    assert applied[:2] == ["create", "verify-created"] and "switch" in applied and applied[-1] == "grant"
    journal = harness.journal()
    assert journal["active"] == desired["fingerprint"]
    assert [e["fingerprint"] for e in journal["pending"]] == [first["fingerprint"]]


def test_apply_saved_refuses_when_the_journal_moved(harness, tmp_path):
    released(harness)
    write_env(harness.env_dir, versions=CHANGE)
    out, _ = _plan(harness, tmp_path)
    assert harness.apply() == 0  # someone applied the change outside the manifest
    write_env(harness.env_dir, versions=CHANGE)
    with pytest.raises(spa.Refused):
        harness.controller().apply_saved(out)


def test_apply_saved_refuses_a_changed_generated_config(harness, tmp_path):
    released(harness)
    write_env(harness.env_dir, versions=CHANGE)
    out, _ = _plan(harness, tmp_path)
    write_env(harness.env_dir, columns=("email", "phone"), classes=("email_address",))
    with pytest.raises(spa.Refused, match="the manifest planned"):
        harness.controller().apply_saved(out)


def test_apply_saved_refuses_a_phase_applied_since_the_plan(harness, tmp_path):
    released(harness)
    write_env(harness.env_dir, versions=CHANGE)
    first, _ = _plan(harness, tmp_path, "first")
    second, _ = _plan(harness, tmp_path, "second")
    assert harness.controller().apply_saved(first) == spa.MORE_PHASES
    with pytest.raises(spa.Refused, match="applied since release-plan"):
        harness.controller().apply_saved(second)


def test_a_saved_plan_whose_render_no_longer_matches_is_never_applied(harness, tmp_path):
    released(harness)
    write_env(harness.env_dir, versions=CHANGE)
    out, phases = _plan(harness, tmp_path)
    sidecar = pg.sidecar_path(out / phases["steps"][0]["file"])
    record = json.loads(sidecar.read_text())
    record["rendered"]["tags"] = {}
    sidecar.write_text(json.dumps(record))
    state = live_state(harness.env_dir)
    assert harness.controller().apply_saved(out) == 1
    assert live_state(harness.env_dir) == state
    assert harness.journal()["transition"]["status"] == "failed"


def test_hooks_never_plan_or_apply_anything_but_the_saved_plan(harness, tmp_path):
    released(harness)
    write_env(harness.env_dir, versions=CHANGE)
    out, phases = _plan(harness, tmp_path)
    before = len(_runner_log(harness))
    assert harness.controller().apply_saved(out) == spa.MORE_PHASES
    calls = _runner_log(harness)[before:]
    assert calls == [f"apply-plan {out / phases['steps'][0]['file']}"], calls


def test_legacy_envs_plan_and_apply_nothing(tmp_path):
    env_dir = tmp_path / "envs" / "dev"
    env_dir.mkdir(parents=True)
    (env_dir / "env.auto.tfvars").write_text('genie_space_id = "x"\n')
    calls = []
    controller = spa.Controller(env_dir, "dev", run=lambda *a, **k: calls.append(a))
    assert controller.plan(tmp_path / "out") == 0 and controller.apply_saved(tmp_path / "out") == 0
    assert calls == [] and not (tmp_path / "out").exists()


def test_cli_exit_code_10_means_more_phases(harness, tmp_path, monkeypatch):
    released(harness)
    write_env(harness.env_dir, versions=CHANGE)
    out, _ = _plan(harness, tmp_path)
    controller = harness.controller()
    monkeypatch.setattr(spa, "Controller", lambda *a, **k: controller)
    assert spa.main(["apply-saved", "--env-dir", str(harness.env_dir), "--plan-dir", str(out)]) == 10
    assert os.environ.get("FAKE_TF_FAIL_APPLY") is None
