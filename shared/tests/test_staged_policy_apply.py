"""Staged policy changes (rollout step 6): the controller, end to end on a fake layer.

The real hooks (scripts/policy_generations.py) render, check and plan each
stage; a fake terraform_layer.sh keeps a Terraform-shaped state file and
applies the saved plan (optionally failing halfway); the real live check
(scripts/staged_policy_verify.py) reads "Unity Catalog" from that state; and
a simulated verify-access queries every tier against the state as Unity
Catalog would evaluate it. After every hook, every governed column is
checked: never unmasked for a principal its old or new protection masks,
never doubly masked outside the principals a tier move is moving.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import hcl2
import pytest

import generate_abac
from governance_policies import build_model, is_never_raw
from scripts import environment_lock
from scripts import policy_generations as pg
from scripts import staged_policy_apply as spa
from scripts import staged_policy_verify as spv
from verify_effective_access import load_spec_from_tfvars, validate_spec_identifiers

SHARED = Path(__file__).resolve().parents[1]
TIERS = ["raw", "regional", "masked"]

FAKE_RUNNER = r'''#!/usr/bin/env python3
"""A data_access terraform_layer.sh: plan/show/apply-plan against a JSON state."""
import json, os, re, sys
import hcl2

layer, env_name, command, *args = sys.argv[1:]
layer_dir = os.environ["LAYER_ENV_DIR"]
# Like terraform_layer.sh: a staged phase's render stands in for the split config.
config_dir = os.environ.get("GENIERAILS_LAYER_CONFIG_DIR") or layer_dir
state_path = os.path.join(layer_dir, "terraform.tfstate")
with open(os.path.join(layer_dir, "..", "runner.log"), "a") as log:
    log.write(" ".join([command] + args) + "\n")
MASK = ("databricks_policy_info", "policies")
TAG = ("databricks_entity_tag_assignment", "treatment")


def load_state():
    if not os.path.exists(state_path):
        return {"version": 4, "resources": [], "functions": []}
    return json.load(open(state_path))


def instances(state, kind):
    for resource in state["resources"]:
        if (resource["type"], resource["name"]) == kind:
            return {i["index_key"]: i["attributes"] for i in resource["instances"]}
    return {}


def desired():
    cfg = hcl2.loads(open(os.path.join(config_dir, "abac.auto.tfvars")).read())
    masks = {}
    for p in cfg.get("fgac_policies") or []:
        if p.get("policy_type") == "POLICY_TYPE_COLUMN_MASK":
            masks[p["name"]] = {
                "catalog": p["catalog"], "to_principals": p["to_principals"],
                "except_principals": p.get("except_principals") or [],
                "column_mask": {"function_name": f"{p['function_catalog']}.{p['function_schema']}.{p['function_name']}"},
                "match_columns": [{"condition": p["match_condition"]}],
            }
    tags = {f"columns|{t['entity_name']}|gr_treatment": {"entity_name": t["entity_name"], "tag_value": t["tag_value"]}
            for t in cfg.get("tag_assignments") or []
            if t["entity_type"] == "columns" and t["tag_key"] == "gr_treatment"}
    sql = open(os.path.join(config_dir, "masking_functions.sql")).read()
    functions, catalog, schema = [], "", ""
    for line in sql.splitlines():
        if line.startswith("USE CATALOG "):
            catalog = line.split()[2].rstrip(";")
        elif line.startswith("USE SCHEMA "):
            schema = line.split()[2].rstrip(";")
        match = re.match(r"CREATE OR REPLACE FUNCTION (\w+)", line)
        if match:
            functions.append(f"{catalog}.{schema}.{match.group(1)}".lower())
    return {MASK: masks, TAG: tags}, functions


def flag(name):
    return next((a.split("=", 1)[1] for a in args if a.startswith(name + "=")), "")


if command == "plan":
    stage = flag("-var").split("=", 1)[1] if flag("-var") else ""
    if os.environ.get("FAKE_TF_FAIL_PLAN") == stage:
        sys.exit(1)
    state = load_state()
    want, functions = desired()
    changes = []
    for kind, wanted in want.items():
        have = instances(state, kind)
        for key in sorted(have.keys() | wanted.keys()):
            if key not in wanted:
                actions = ["delete"]
            elif key not in have:
                actions = ["create"]
            elif have[key] != wanted[key]:
                actions = ["update"]
            else:
                continue
            changes.append({"address": f'module.data_access.{kind[0]}.{kind[1]}["{key}"]', "type": kind[0],
                            "name": kind[1], "index": key, "change": {"actions": actions},
                            "after": wanted.get(key)})
    json.dump({"stage": stage, "resource_changes": changes, "functions": functions}, open(flag("-out"), "w"))
elif command == "show":
    print("+ terraform show")
    print(json.dumps(json.load(open(args[-1]))))
elif command == "apply-plan":
    plan = json.load(open(args[-1]))
    state = load_state()
    changes = plan["resource_changes"]
    fail = os.environ.get("FAKE_TF_FAIL_APPLY") == plan["stage"]
    if fail:
        changes = changes[: len(changes) // 2]
    state["functions"] = sorted(set(state.get("functions") or []) | set(plan["functions"]))
    for change in changes:
        kind = (change["type"], change["name"])
        resource = next((r for r in state["resources"] if (r["type"], r["name"]) == kind), None)
        if resource is None:
            resource = {"module": "module.data_access", "mode": "managed", "type": kind[0], "name": kind[1],
                        "instances": []}
            state["resources"].append(resource)
        resource["instances"] = [i for i in resource["instances"] if i["index_key"] != change["index"]]
        if change["change"]["actions"] != ["delete"]:
            resource["instances"].append({"index_key": change["index"], "attributes": change["after"]})
    json.dump(state, open(state_path, "w"), indent=1, sort_keys=True)
    sys.exit(1 if fail else 0)
else:
    sys.exit(f"unsupported {command}")
'''


# -- the env -------------------------------------------------------------------
def write_env(env_dir: Path, *, tiers=TIERS, versions=None, columns=("email",), classes=("email_address",),
              mode="deterministic", exempt=(), deployer=""):
    """Write a generated deterministic config (and its env) and return its generation."""
    env_dir.mkdir(parents=True, exist_ok=True)
    settings = {"access_tier_groups": list(tiers), "treatment_versions": versions or {},
                "raw_exempt_principals": list(exempt)}
    model = build_model({f"cat.sch.t.{c}": list(classes) for c in columns},
                        {f"cat.sch.t.{c}": "STRING" for c in columns}, settings,
                        deployer_principal=deployer)
    generated = env_dir / "generated"
    generated.mkdir(exist_ok=True)
    (generated / "abac.auto.tfvars").write_text("tag_policies = [\n]\ntag_assignments = [\n]\nfgac_policies = [\n]\n")
    (generated / "masking_functions.sql").write_text("")
    generate_abac.write_deterministic_model(generated / "abac.auto.tfvars", generated / "masking_functions.sql", model)
    (env_dir / "env.auto.tfvars").write_text(
        f'governance_mode = "{mode}"\n'
        + f"access_tier_groups = {json.dumps(list(tiers))}\n"
        + f"raw_exempt_principals = {json.dumps(list(exempt))}\n"
        + (f'deployer_principal_id = "{deployer}"\n' if deployer else "")
        + (f"treatment_versions = {spa._hcl_value(versions)}\n" if versions else ""))
    layer = env_dir / "data_access"
    layer.mkdir(exist_ok=True)
    # The promote split: the layer starts as the generated config.
    (layer / "abac.auto.tfvars").write_text((generated / "abac.auto.tfvars").read_text())
    (layer / "masking_functions.sql").write_text((generated / "masking_functions.sql").read_text())
    return pg.desired_generation(env_dir) if mode == "deterministic" else None


def live_state(env_dir: Path) -> dict:
    path = env_dir / "data_access" / "terraform.tfstate"
    return json.loads(path.read_text()) if path.is_file() else {"resources": [], "functions": []}


def _instances(state, rtype, name):
    for resource in state["resources"]:
        if (resource["type"], resource["name"]) == (rtype, name):
            return {i["index_key"]: i["attributes"] for i in resource["instances"]}
    return {}


def live_policies(state) -> list[dict]:
    return [{"name": key, **attrs} for key, attrs in _instances(state, "databricks_policy_info", "policies").items()]


def live_tags(state) -> dict[str, str]:
    return {a["entity_name"]: a["tag_value"]
            for a in _instances(state, "databricks_entity_tag_assignment", "treatment").values()}


def fake_reader(env_dir, rendered, columns):
    """What staged_policy_verify reads from Unity Catalog, from the fake state."""
    state = live_state(env_dir)
    policies = [{"name": f"{p['catalog']}_{p['name']}", "policy_type": "COLUMN_MASK",
                 "to_principals": p["to_principals"], "except_principals": p["except_principals"],
                 "column_mask": p["column_mask"], "match_columns": p["match_columns"],
                 "on_securable_fullname": p["catalog"]} for p in live_policies(state)]
    by_catalog = {}
    for policy in policies:
        by_catalog.setdefault(policy["on_securable_fullname"], []).append(policy)
    catalogs = {p["catalog"] for p in rendered["fgac_policies"]} | {c.split(".")[0] for c in columns}
    tables = {".".join(c.split(".")[:3]) for c in columns}
    return ({c: by_catalog.get(c, []) for c in catalogs},
            {t: by_catalog.get(t.split(".")[0], []) for t in tables},
            set(state.get("functions") or []),
            {c.lower(): v for c, v in live_tags(state).items()})


def _value_of(policy):
    return pg.policy_value({"match_condition": policy["match_columns"][0]["condition"]})


def _covers(policy, who):
    to, excepted = policy["to_principals"], policy["except_principals"]
    if who == pg.OUTSIDER:
        return "account users" in to
    return ("account users" in to or who in to) and who not in excepted


def expected_access(who: str, value: str, tiers: list[str], exempt: list[str]) -> str:
    if is_never_raw(value):
        return "full"
    if who in tiers[:1] or who in exempt:
        return "raw"
    if who in tiers[1:-1]:
        return "partial"
    return "full"


def simulated_verify_access(env_dir: Path, tfvars: Path, env_file: Path) -> list[str]:
    """verify-access, as Unity Catalog would answer each tier's query on the fake state."""
    cfg = hcl2.loads(tfvars.read_text())
    env = hcl2.loads(env_file.read_text())
    tiers, exempt = list(env.get("access_tier_groups") or []), list(env.get("raw_exempt_principals") or [])
    state = live_state(env_dir)
    tags = live_tags(state)
    problems = []
    for item in cfg.get("tag_assignments") or []:
        if item["tag_key"] != "gr_treatment":
            continue
        column, value = item["entity_name"], item["tag_value"]
        if tags.get(column) != value:
            problems.append(f"{column}: live tag {tags.get(column)}, checked {value}")
            continue
        masks = [p for p in live_policies(state) if _value_of(p) == value]
        for who in tiers + exempt + [pg.OUTSIDER]:
            applied = [p for p in masks if _covers(p, who)]
            if len(applied) > 1:
                problems.append(f"{who} on {column}: MULTIPLE_MASKS")
                continue
            got = "raw" if not applied else applied[0]["name"].rsplit("_", 1)[1]
            want = expected_access(who, value, tiers, exempt)
            if got != want:
                problems.append(f"{who} on {column}: sees {got}, expected {want}")
    return problems


class Harness:
    """Runs the controller with in-process hooks against the fake layer."""

    def __init__(self, env_dir: Path, monkeypatch, tmp_path: Path):
        self.env_dir = env_dir
        runner = tmp_path / "fake_terraform_layer.py"
        runner.write_text(FAKE_RUNNER)
        runner.chmod(0o755)
        monkeypatch.setattr(pg, "RUNNER", runner)
        self.calls: list[str] = []
        self.verify_envs: list[dict] = []
        self.verify_specs = []
        self.validate_real_specs = False
        self.fail_verify: set[str] = set()
        self.crash_after: str = ""
        self.invariant_problems: list[str] = []
        self.dropped: list[str] = []
        # stage -> edit of the fake live state right after that stage's apply "succeeded"
        # (Unity Catalog diverging from what Terraform says it applied).
        self.diverge_after: dict = {}
        self.inventory_problems: list[list[str]] = []
        # "strict": the live check must find the state exactly as rendered, so no
        # failure-path test can pass on an unexpected divergence. "real": the
        # controller gets the real check's result (a test of the check itself).
        self.live_check = "strict"
        self.simulate_access = True
        monkeypatch.delenv("FAKE_TF_FAIL_APPLY", raising=False)
        monkeypatch.delenv("FAKE_TF_FAIL_PLAN", raising=False)

    def controller(self) -> spa.Controller:
        return spa.Controller(self.env_dir, "dev", hook_dir=Path("/hooks"), verify_access_cmd="simulated",
                              inventory_cmd=["inventory"], commit="abc123", run=self.run)

    def run(self, argv, env=None, check=False):
        env = dict(env or {})
        if argv[0].startswith("/hooks/"):
            stage = argv[0].rsplit("/", 1)[1]
            # abandon runs twice: a saved plan, then exactly that plan.
            self.calls.append(stage + ({"plan": ":plan", "saved": ":saved"}.get(
                "plan" if env.get(pg.PLAN_OUT_ENV) else "saved" if env.get(pg.SAVED_PLAN_ENV) else "", "")
                if stage == "abandon" else ""))
            before = live_state(self.env_dir)
            if stage == "drop-functions":
                rc = pg.drop_functions(self.env_dir, env | {"GENIERAILS_WAREHOUSE_ID": "wh"},
                                       live_users=lambda *_: set(), run=self._drop)
            else:
                # The fake runner runs as a subprocess; it sees the failure switches.
                os.environ.update({k: v for k, v in env.items() if k.startswith("FAKE_TF")})
                try:
                    rc = pg.run_hook(stage, env)
                except pg.StageError as exc:
                    print(exc, file=sys.stderr)
                    rc = 2
            self._check_invariants(stage, before, env)
            if stage in self.diverge_after:
                path = self.env_dir / "data_access" / "terraform.tfstate"
                state = live_state(self.env_dir)
                self.diverge_after.pop(stage)(state)
                path.write_text(json.dumps(state))
            if self.crash_after == stage:
                self.crash_after = ""
                raise KeyboardInterrupt(f"crash after {stage}")
            return SimpleNamespace(returncode=rc)
        if argv[0] == "inventory":
            stages = set(argv[argv.index("--stage") + 1].split(","))
            self.calls.append("inventory:" + "/".join(sorted(stages)))
            # The real live check, reading "Unity Catalog" from the fake state.
            problems = spv.verify(self.env_dir, stages, reader=fake_reader)
            self.inventory_problems.append(problems)
            if self.live_check == "strict":
                assert not problems, problems
            return SimpleNamespace(returncode=1 if problems else 0)
        if argv[:2] == ["/bin/sh", "-c"]:
            record = pg.load_stage_record(self.env_dir)
            self.calls.append(f"verify-access:{record['stage']}")
            self.verify_envs.append(hcl2.loads(Path(env["GENIERAILS_VERIFY_ENV_FILE"]).read_text()))
            if self.validate_real_specs:
                spec = load_spec_from_tfvars(
                    Path(env["GENIERAILS_VERIFY_TFVARS"]),
                    env_file=Path(env["GENIERAILS_VERIFY_ENV_FILE"]), key_column="id")
                validate_spec_identifiers(spec)
                self.verify_specs.append(spec)
            problems = [] if not self.simulate_access else simulated_verify_access(
                self.env_dir, Path(env["GENIERAILS_VERIFY_TFVARS"]),
                Path(env["GENIERAILS_VERIFY_ENV_FILE"]))
            if record["stage"] in self.fail_verify:
                self.fail_verify.discard(record["stage"])
                problems.append("injected verification failure")
            assert not problems or record["stage"] in ("switch", "create", "restore") and \
                "injected verification failure" in problems, problems
            return SimpleNamespace(returncode=1 if problems else 0)
        raise AssertionError(f"unexpected command {argv}")

    def _drop(self, argv, check=False):
        sql = Path(argv[argv.index("--sql-file") + 1]).read_text()
        self.dropped += [line.split()[4].split("(")[0] for line in sql.splitlines() if line.startswith("CREATE")]
        return SimpleNamespace(returncode=0)

    def _check_invariants(self, stage, before, env):
        """After each stage, nobody sees a governed column raw unless that stage's target makes them
        raw (old protection before the switch, both while tightening, desired after), and nobody
        gets two masks outside the principals a tier move is moving."""
        state = live_state(self.env_dir)
        old = pg.load_snapshot(self.env_dir, env.get("GENIERAILS_OLD_POLICY_FINGERPRINT", ""))
        desired = pg.load_snapshot(self.env_dir, env.get("GENIERAILS_POLICY_FINGERPRINT", ""))

        def access(generation, who, value):
            settings = (generation or {}).get("settings") or {}
            return expected_access(who, value, settings.get("access_tier_groups") or [],
                                   settings.get("raw_exempt_principals") or [])

        targets = {"create": [old], "restore": [old], "abandon": [old], "tighten": [old, desired],
                   "switch": [desired], "retire": [desired]}.get(stage, [])
        universe = pg._universe(old, desired) | {pg.OUTSIDER}
        for column, value in live_tags(state).items():
            masks = [p for p in live_policies(state) if _value_of(p) == value]
            if not masks:
                self.invariant_problems.append(f"after {stage}: {column} carries {value} with no policy")
            for who in universe:
                applied = [p for p in masks if _covers(p, who)]
                gens = [g for g in targets if g] or [desired]
                if not applied and any(access(g, who, value) != "raw" for g in gens):
                    self.invariant_problems.append(f"after {stage}: {who} sees {column} raw")
                moving = bool(old and desired) and access(old, who, value) != access(desired, who, value)
                if len(applied) > 1 and not (stage == "tighten" and moving):
                    self.invariant_problems.append(f"after {stage}: {who} has {len(applied)} masks on {column}")

    def apply(self, **fail) -> int:
        for key in ("FAKE_TF_FAIL_APPLY", "FAKE_TF_FAIL_PLAN"):
            os.environ.pop(key, None)
        for key, stage in fail.items():
            os.environ[key] = stage
        try:
            return self.controller().run()
        finally:
            for key in fail:
                os.environ.pop(key, None)

    def journal(self) -> dict:
        return spa.load_journal(self.env_dir)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    env_dir = tmp_path / "envs" / "dev"
    env_dir.mkdir(parents=True)
    h = Harness(env_dir, monkeypatch, tmp_path)
    yield h
    assert not h.invariant_problems, h.invariant_problems


def released(h, **env) -> dict:
    """A first generation, released and confirmed."""
    gen = write_env(h.env_dir, **env)
    assert h.apply() == 0
    assert h.controller().confirm() == 0
    h.calls.clear()
    h.verify_envs.clear()
    return gen


def new_value(gen_a, gen_b):
    return set(gen_b["tags"].values()) - set(gen_a["tags"].values())


# -- legacy ----------------------------------------------------------------------
def test_legacy_env_is_never_staged_and_changes_nothing(tmp_path, monkeypatch, capsys):
    env_dir = tmp_path / "envs" / "dev"
    env_dir.mkdir(parents=True)
    (env_dir / "env.auto.tfvars").write_text('genie_space_id = "x"\n')  # governance_mode unset: legacy
    calls = []
    controller = spa.Controller(env_dir, "dev", run=lambda *a, **k: calls.append(a))
    for command in ("run", "confirm", "abandon", "describe"):
        assert getattr(controller, command)() == 0
    assert calls == []
    assert not (env_dir / "data_access").exists() and not (env_dir / "generated").exists()
    assert "legacy governance mode" in capsys.readouterr().out
    # Not even a lock or a journal is written for a legacy env.
    assert not (env_dir / environment_lock.LOCK_RELPATH).exists()


def test_legacy_env_cli_runs_without_hcl_or_terraform_side_effects(tmp_path):
    env_dir = tmp_path / "envs" / "dev"
    env_dir.mkdir(parents=True)
    (env_dir / "env.auto.tfvars").write_text('governance_mode = "legacy"\n')
    for command in ("run", "confirm", "status", "describe", "abandon"):
        result = subprocess.run([sys.executable, str(SHARED / "scripts" / "staged_policy_apply.py"), command,
                                 "--env-dir", str(env_dir), "--commit", "x"], text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        assert "Traceback" not in result.stderr
    assert sorted(p.name for p in env_dir.iterdir()) == ["env.auto.tfvars"]


# -- the protocol ---------------------------------------------------------------------
def test_first_release_creates_policies_before_tagging_any_column(harness):
    gen = write_env(harness.env_dir)
    assert harness.apply() == 0
    assert harness.calls == ["create", "inventory:create/restore", "switch", "inventory:switch",
                             "verify-access:switch", "grant"]
    journal = harness.journal()
    assert journal["active"] == gen["fingerprint"] and journal["pending"] == [] and journal["transition"] is None
    assert live_tags(live_state(harness.env_dir)) == gen["tags"]


def test_unchanged_generation_is_one_apply_with_nothing_to_verify_or_retire(harness):
    released(harness)
    assert harness.apply() == 0
    assert harness.calls == ["create", "grant"]


def test_treatment_change_creates_verifies_switches_verifies_and_keeps_the_old_generation(harness):
    old = released(harness)
    new = write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}})
    assert new_value(old, new)
    assert harness.apply() == 0
    assert harness.calls == ["create", "inventory:create/restore", "verify-access:create",
                             "switch", "inventory:switch", "verify-access:switch", "grant"]
    state = live_state(harness.env_dir)
    names = {p["name"] for p in live_policies(state)}
    # Both generations are live; columns carry the new one; nothing was retired.
    assert {p["name"] for p in old["fgac_policies"] + new["fgac_policies"]} == names
    assert live_tags(state) == new["tags"]
    journal = harness.journal()
    assert journal["active"] == new["fingerprint"]
    assert journal["pending"] == [{"fingerprint": old["fingerprint"], "confirmed": False,
                                   "superseded_by": journal["history"][-1]["run_id"]}]


def test_old_generation_retires_only_after_a_later_confirmed_release(harness):
    old = released(harness)
    new = write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}})
    assert harness.apply() == 0
    harness.calls.clear()
    # The same release again (a retry, or rehearse re-run) before confirmation: no retire.
    assert harness.apply() == 0
    assert "retire" not in harness.calls
    assert harness.journal()["pending"][0]["confirmed"] is False
    assert harness.controller().confirm() == 0
    harness.calls.clear()
    # A later release: verify the live generation first, then retire the old one.
    assert harness.apply() == 0
    assert harness.calls == ["create", "inventory:create/restore", "verify-access:create",
                             "retire", "inventory:retire", "drop-functions", "grant"]
    state = live_state(harness.env_dir)
    assert {p["name"] for p in live_policies(state)} == {p["name"] for p in new["fgac_policies"]}
    assert harness.journal()["pending"] == []
    old_only = {p["function_name"] for p in old["fgac_policies"]} - {p["function_name"] for p in new["fgac_policies"]}
    assert old_only and set(harness.dropped) == old_only


def test_a_change_never_retires_the_generation_it_switched_away_from(harness):
    first = released(harness)
    second = write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}})
    assert harness.apply() == 0 and harness.controller().confirm() == 0
    third = write_env(harness.env_dir, versions={"email_partial": {"partial": "initials"}})
    harness.calls.clear()
    assert harness.apply() == 0
    # first (superseded by an earlier, confirmed release) retires; second stays.
    history = harness.journal()["history"][-1]
    assert history["retired"] == first["fingerprint"] and history["from"] == second["fingerprint"]
    names = {p["name"] for p in live_policies(live_state(harness.env_dir))}
    assert {p["name"] for p in second["fgac_policies"] + third["fgac_policies"]} == names
    assert harness.journal()["pending"][0]["fingerprint"] == second["fingerprint"]


def test_an_unconfirmed_change_blocks_a_new_change(harness):
    released(harness)
    write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}})
    assert harness.apply() == 0  # never confirmed (its release failed later)
    write_env(harness.env_dir, versions={"email_partial": {"partial": "initials"}})
    harness.calls.clear()
    with pytest.raises(spa.Refused, match="no successful release has confirmed"):
        harness.apply()
    assert harness.calls == []


# -- failure paths ------------------------------------------------------------------------
def test_failed_switch_apply_restores_the_previous_protection(harness):
    old = released(harness)
    new = write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}}, columns=("email", "contact"))
    assert harness.apply(FAKE_TF_FAIL_APPLY="switch") == 1
    assert harness.calls[-2:] == ["restore", "inventory:restore"]
    state = live_state(harness.env_dir)
    # Every column the old generation governs is back on it.
    assert {c: v for c, v in live_tags(state).items() if c in old["tags"]} == old["tags"]
    t = harness.journal()["transition"]
    assert t["status"] == "failed" and t["failed_phase"] == "switch" and t["completed"] == ["create"]
    assert harness.journal()["active"] == old["fingerprint"]
    # The retry resumes after create and finishes; the old generation is not retired.
    harness.calls.clear()
    assert harness.apply() == 0
    assert harness.calls[0] == "inventory:create/restore" and "retire" not in harness.calls
    assert live_tags(live_state(harness.env_dir)) == new["tags"]


def test_failed_verification_after_switch_restores_and_the_retry_switches_again(harness):
    old = released(harness)
    new = write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}})
    harness.fail_verify = {"switch"}
    assert harness.apply() == 1
    assert live_tags(live_state(harness.env_dir)) == old["tags"]
    assert harness.journal()["transition"]["switch_attempted"] is True
    assert harness.apply() == 0
    assert live_tags(live_state(harness.env_dir)) == new["tags"]
    assert harness.journal()["pending"][0]["fingerprint"] == old["fingerprint"]


def _retag_first_column(value):
    """Edit a state: the first column's treatment tag is ``value``, whatever the apply reported."""
    def edit(state):
        for resource in state["resources"]:
            if resource["name"] == "treatment":
                resource["instances"][0]["attributes"]["tag_value"] = value
    return edit


def test_live_state_diverging_after_switch_is_caught_by_the_real_live_check_and_restored(harness):
    old = released(harness)
    new = write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}})
    # Only the real staged_policy_verify can notice: verify-access is not simulated here.
    harness.simulate_access = False
    harness.live_check = "real"
    harness.diverge_after = {"switch": _retag_first_column(next(iter(old["tags"].values())))}
    assert harness.apply() == 1
    assert "verify-access:switch" not in harness.calls  # stopped by the live check, before verify-access
    assert any("live treatment" in p for p in harness.inventory_problems[-2]), harness.inventory_problems
    assert harness.calls[-2:] == ["restore", "inventory:restore"]
    t = harness.journal()["transition"]
    assert t["status"] == "failed" and t["failed_phase"] == "verify-switched" and t["completed"] == ["create"]
    assert harness.journal()["active"] == old["fingerprint"]
    # The previous protection is back: the old generation's values and policies.
    state = live_state(harness.env_dir)
    assert live_tags(state) == old["tags"]
    assert {p["name"] for p in old["fgac_policies"]} <= {p["name"] for p in live_policies(state)}
    # Fixed (UC applies what it is told): the retry resumes after create and finishes.
    harness.calls.clear()
    assert harness.apply() == 0
    assert harness.calls[0] == "inventory:create/restore" and "switch" in harness.calls
    assert live_tags(live_state(harness.env_dir)) == new["tags"]
    assert harness.journal()["transition"] is None and harness.journal()["active"] == new["fingerprint"]


def test_failed_create_leaves_everything_live_and_resumes_at_create(harness):
    old = released(harness)
    new = write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}})
    assert harness.apply(FAKE_TF_FAIL_APPLY="create") == 1
    assert "restore" not in harness.calls
    state = live_state(harness.env_dir)
    assert live_tags(state) == old["tags"]
    assert {p["name"] for p in old["fgac_policies"]} <= {p["name"] for p in live_policies(state)}
    harness.calls.clear()
    assert harness.apply() == 0
    assert harness.calls[0] == "create"
    assert live_tags(live_state(harness.env_dir)) == new["tags"]


def test_failed_verification_after_create_never_switches(harness):
    old = released(harness)
    write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}})
    harness.fail_verify = {"create"}
    assert harness.apply() == 1
    assert "switch" not in harness.calls
    assert live_tags(live_state(harness.env_dir)) == old["tags"]


def test_crash_after_switch_resumes_from_the_journal(harness):
    old = released(harness)
    new = write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}})
    harness.crash_after = "switch"
    with pytest.raises(KeyboardInterrupt):
        harness.apply()
    t = harness.journal()["transition"]
    assert t["status"] == "running" and t["current"] == "switch" and "switch" not in t["completed"]
    harness.calls.clear()
    assert harness.apply() == 0
    assert harness.calls[0] == "switch"  # re-run (idempotent), then verified
    assert "verify-access:switch" in harness.calls and "retire" not in harness.calls
    assert harness.journal()["pending"][0]["fingerprint"] == old["fingerprint"]
    assert live_tags(live_state(harness.env_dir)) == new["tags"]


def test_crash_during_restore_is_finished_before_anything_else(harness):
    old = released(harness)
    write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}})
    harness.fail_verify = {"switch"}
    harness.crash_after = "restore"
    with pytest.raises(KeyboardInterrupt):
        harness.apply()
    assert harness.journal()["transition"]["restoring"] is True
    harness.calls.clear()
    assert harness.apply() == 0
    assert harness.calls[:2] == ["restore", "inventory:restore"]
    assert harness.journal()["pending"][0]["fingerprint"] == old["fingerprint"]


def test_a_new_config_is_refused_while_a_change_is_unfinished(harness):
    released(harness)
    write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}})
    assert harness.apply(FAKE_TF_FAIL_APPLY="switch") == 1
    before = harness.journal()
    write_env(harness.env_dir, versions={"email_partial": {"partial": "initials"}})
    harness.calls.clear()
    with pytest.raises(spa.Refused, match="can't start until this one is finished"):
        harness.apply()
    assert harness.calls == [] and harness.journal() == before


def test_abandon_drops_a_change_that_never_switched(harness):
    old = released(harness)
    failed = write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}})
    harness.fail_verify = {"create"}
    assert harness.apply() == 1
    assert harness.controller().abandon() == 0
    state = live_state(harness.env_dir)
    assert {p["name"] for p in live_policies(state)} == {p["name"] for p in old["fgac_policies"]}
    assert harness.journal()["transition"] is None and harness.journal()["active"] == old["fingerprint"]
    fixed = write_env(harness.env_dir, versions={"email_partial": {"partial": "initials"}})
    assert fixed["fingerprint"] != failed["fingerprint"]
    assert harness.apply() == 0
    assert live_tags(live_state(harness.env_dir)) == fixed["tags"]


def test_abandon_is_refused_once_a_switch_was_attempted(harness):
    released(harness)
    write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}})
    # The switch fails halfway and so does the restore: columns may be on either generation.
    assert harness.apply(FAKE_TF_FAIL_APPLY="switch", FAKE_TF_FAIL_PLAN="restore") == 1
    t = harness.journal()["transition"]
    assert t["switch_attempted"] and t["restore_failed"] and not t["restored"]
    harness.calls.clear()
    with pytest.raises(spa.Refused, match="can't be abandoned"):
        harness.controller().abandon()
    assert harness.calls == [] and harness.journal()["transition"] == t


def _rolled_back_switch(harness):
    """The live AWS shape: verify-switched failed and the previous protection was restored and verified."""
    old = released(harness)
    failed = write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}})
    harness.fail_verify = {"switch"}
    assert harness.apply() == 1
    t = harness.journal()["transition"]
    assert (t["completed"], t["failed_phase"], t["switch_attempted"]) == (["create"], "verify-switched", True)
    assert t["restored"]["after"] == "verify-switched" and t["restored"]["generation"] == old["fingerprint"]
    assert t["restored"]["tags"] == old["tags"]
    assert live_tags(live_state(harness.env_dir)) == old["tags"]
    harness.calls.clear()
    return old, failed


def test_abandon_drops_a_rolled_back_switch_after_a_live_check(harness):
    old, failed = _rolled_back_switch(harness)
    # The deployer and the tier groups were recreated: the config changed, so the change can't resume.
    fixed = write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}},
                      tiers=["raw_v2", "regional_v2", "masked_v2"], exempt=("new-deployer-sp",))
    assert fixed["fingerprint"] != failed["fingerprint"]
    with pytest.raises(spa.Refused, match="make policy-abandon ENV=dev"):
        harness.apply()
    harness.calls.clear()
    before = {p["name"]: p for p in live_policies(live_state(harness.env_dir))}
    created = {p["name"] for p in failed["fgac_policies"]} - {p["name"] for p in old["fgac_policies"]}
    assert created and created <= before.keys()
    assert harness.controller().abandon() == 0
    # Plan, then the fresh live check, then exactly that plan, then the check of what it left.
    assert harness.calls == ["abandon:plan", "inventory:restore", "abandon:saved", "inventory:abandon"]
    state = live_state(harness.env_dir)
    after = {p["name"]: p for p in live_policies(state)}
    assert before.keys() - after.keys() == created
    assert after == {name: policy for name, policy in before.items() if name not in created}
    assert {p["name"] for p in live_policies(state)} == {p["name"] for p in old["fgac_policies"]}
    assert live_tags(state) == old["tags"]
    journal = harness.journal()
    assert journal["transition"] is None and journal["active"] == old["fingerprint"]
    assert journal["history"][-1]["abandoned"] == failed["fingerprint"]
    assert harness.apply() == 0
    assert live_tags(live_state(harness.env_dir)) == fixed["tags"]
    assert harness.journal()["active"] == fixed["fingerprint"]


def _retag_live(env_dir, column, value):
    """Retag a column in "Unity Catalog" (the fake state) outside GenieRails."""
    path = env_dir / "data_access" / "terraform.tfstate"
    state = live_state(env_dir)
    for resource in state["resources"]:
        for instance in resource["instances"]:
            if instance["attributes"].get("entity_name") == column:
                instance["attributes"]["tag_value"] = value
    path.write_text(json.dumps(state))


def _new_value_column(old, failed):
    (column, value), = ((c, v) for c, v in failed["tags"].items() if v != old["tags"].get(c))
    return column, value


def _after_call(harness, call, action):
    """Run ``action(env)`` right after the harness call ``call`` returns (Unity Catalog moving mid-abandon)."""
    real_run = harness.run

    def run(argv, env=None, check=False):
        result = real_run(argv, env=env, check=check)
        if harness.calls and harness.calls[-1] == call:
            action(env or {})
        return result

    harness.run = run


def test_abandon_refuses_a_column_retagged_before_the_plan(harness, capsys):
    old, failed = _rolled_back_switch(harness)
    column, value = _new_value_column(old, failed)
    _retag_live(harness.env_dir, column, value)  # after the restore, before the abandon plan
    harness.live_check = "real"
    before, policies = harness.journal(), live_policies(live_state(harness.env_dir))
    capsys.readouterr()
    with pytest.raises(spa.Refused, match="abandon plan was refused"):
        harness.controller().abandon()
    # The refreshed plan sees the column moving back as a tag update: refused before any live check or apply.
    assert harness.calls == ["abandon:plan"]
    assert f"columns|{column}|gr_treatment" in capsys.readouterr().err
    assert harness.journal() == before and live_policies(live_state(harness.env_dir)) == policies


def test_abandon_refuses_a_column_retagged_between_the_plan_and_the_live_check(harness):
    old, failed = _rolled_back_switch(harness)
    column, value = _new_value_column(old, failed)
    _after_call(harness, "abandon:plan", lambda env: _retag_live(harness.env_dir, column, value))
    harness.live_check = "real"
    before, policies = harness.journal(), live_policies(live_state(harness.env_dir))
    with pytest.raises(spa.Refused, match=r"can't be abandoned \(the live check found"):
        harness.controller().abandon()
    assert harness.calls == ["abandon:plan", "inventory:restore"]
    assert any(f"live treatment {value}" in p for p in harness.inventory_problems[-1])
    assert harness.journal() == before and live_policies(live_state(harness.env_dir)) == policies


def test_abandon_refuses_a_plan_that_updates_a_policy(harness, capsys):
    old, _ = _rolled_back_switch(harness)
    kept = old["fgac_policies"][0]["name"]
    path = harness.env_dir / "data_access" / "terraform.tfstate"
    state = live_state(harness.env_dir)
    for resource in state["resources"]:
        for instance in resource["instances"]:
            if instance["index_key"] == kept:
                instance["attributes"]["except_principals"] = ["someone-added-in-the-ui"]
    path.write_text(json.dumps(state))
    harness.live_check = "real"
    before = harness.journal()
    capsys.readouterr()
    with pytest.raises(spa.Refused, match="abandon plan was refused"):
        harness.controller().abandon()
    assert harness.calls == ["abandon:plan"] and harness.journal() == before
    assert f'["{kept}"]: update' in capsys.readouterr().err
    assert live_policies(live_state(harness.env_dir)) == live_policies(state)


def test_abandon_refuses_a_saved_plan_that_changed_after_it_was_made(harness):
    _rolled_back_switch(harness)
    policies = live_policies(live_state(harness.env_dir))

    before = harness.journal()

    def tamper(env):  # after the controller recorded the plan's hash
        plan = pg.ledger_dir(harness.env_dir) / "abandon" / f"{before['transition']['run_id']}.tfplan"
        plan.write_bytes(plan.read_bytes() + b"\n")

    _after_call(harness, "inventory:restore", tamper)
    with pytest.raises(spa.Refused, match="changed after it was made"):
        harness.controller().abandon()
    assert harness.calls == ["abandon:plan", "inventory:restore"] and harness.journal() == before
    assert live_policies(live_state(harness.env_dir)) == policies


def test_abandon_refuses_a_saved_plan_deleting_a_policy_the_change_did_not_create(harness):
    old, _ = _rolled_back_switch(harness)

    def foreign(env):
        side = pg.sidecar_path(env[pg.PLAN_OUT_ENV])
        record = json.loads(side.read_text())
        record["policy_deletes"].append(old["fgac_policies"][0]["name"])
        side.write_text(json.dumps(record))

    _after_call(harness, "abandon:plan", foreign)
    before = harness.journal()
    with pytest.raises(spa.Refused, match="didn't create"):
        harness.controller().abandon()
    assert harness.calls == ["abandon:plan"] and harness.journal() == before


def test_a_failed_check_after_the_abandon_apply_leaves_the_change_recorded(harness, capsys):
    old, failed = _rolled_back_switch(harness)
    column, value = _new_value_column(old, failed)
    # Unity Catalog moves after the last check before the apply: only the check after it can see it.
    _after_call(harness, "inventory:restore", lambda env: _retag_live(harness.env_dir, column, value))
    harness.live_check = "real"
    capsys.readouterr()
    assert harness.controller().abandon() == 1
    assert harness.calls == ["abandon:plan", "inventory:restore", "abandon:saved", "inventory:abandon"]
    err = capsys.readouterr().err
    assert "ABANDON INCOMPLETE" in err and "re-run make policy-abandon ENV=dev" in err
    journal = harness.journal()
    t = journal["transition"]
    assert t and t["abandon_incomplete"] and t["status"] == "failed" and t["failed_phase"] == "verify-switched"
    assert not any("abandoned" in entry for entry in journal["history"])
    assert "not abandoned" in harness.controller().describe_transition(t)
    # The damage the check reports (the retag outside GenieRails), not something the abandon did.
    assert f"after abandon: {column} carries {value} with no policy" in harness.invariant_problems
    assert all(p.startswith("after abandon:") and column in p for p in harness.invariant_problems)
    harness.invariant_problems.clear()
    # The printed next step: put the column back, re-run abandon.
    _retag_live(harness.env_dir, column, old["tags"][column])
    harness.run = Harness.run.__get__(harness)
    harness.calls.clear()
    assert harness.controller().abandon() == 0
    assert harness.journal()["transition"] is None
    assert live_tags(live_state(harness.env_dir)) == old["tags"]


def test_abandon_of_a_rolled_back_switch_refuses_when_the_live_check_cannot_run(harness):
    old, _ = _rolled_back_switch(harness)
    real_run = harness.run

    def unreadable(argv, env=None, check=False):
        if argv[0] == "inventory":
            harness.calls.append("inventory:unreadable")
            return SimpleNamespace(returncode=2)
        return real_run(argv, env=env, check=check)

    harness.run = unreadable
    before = harness.journal()
    with pytest.raises(spa.Refused, match="live check couldn't run"):
        harness.controller().abandon()
    assert harness.calls == ["abandon:plan", "inventory:unreadable"] and harness.journal() == before
    assert live_tags(live_state(harness.env_dir)) == old["tags"]


def _pre_marker_journal(harness, **shape):
    """The rolled-back switch as a journal written before the restored marker existed."""
    journal = harness.journal()
    journal["transition"].pop("restored")
    journal["transition"].update(shape)
    spa.save_journal(harness.env_dir, journal)
    return journal


PRE_MARKER_MESSAGE = ("journal predates the restore marker; treating the recorded failed switch as restored "
                      "after a fresh live check")


def test_abandon_of_a_pre_marker_journal_runs_every_check_and_drops_the_change(harness, capsys):
    old, failed = _rolled_back_switch(harness)
    _pre_marker_journal(harness)
    capsys.readouterr()
    assert harness.controller().abandon() == 0
    assert PRE_MARKER_MESSAGE in capsys.readouterr().out
    assert harness.calls == ["abandon:plan", "inventory:restore", "abandon:saved", "inventory:abandon"]
    state = live_state(harness.env_dir)
    assert {p["name"] for p in live_policies(state)} == {p["name"] for p in old["fgac_policies"]}
    assert live_tags(state) == old["tags"]
    journal = harness.journal()
    assert journal["transition"] is None and journal["history"][-1]["abandoned"] == failed["fingerprint"]


def test_abandon_of_a_pre_marker_journal_refuses_when_a_column_is_live_on_the_new_value(harness):
    old, failed = _rolled_back_switch(harness)
    before = _pre_marker_journal(harness)
    column, value = _new_value_column(old, failed)
    _after_call(harness, "abandon:plan", lambda env: _retag_live(harness.env_dir, column, value))
    harness.live_check = "real"
    with pytest.raises(spa.Refused, match=r"can't be abandoned \(the live check found"):
        harness.controller().abandon()
    assert harness.calls == ["abandon:plan", "inventory:restore"] and harness.journal() == before


@pytest.mark.parametrize("shape", [
    {"completed": ["create", "verify-created", "switch"]},
    {"completed": []},
    {"restoring": True},
    {"restore_failed": True},
    {"failed_phase": "tighten"},
    {"failed_phase": "grant"},
    {"restored": None},  # a journal from after the marker existed: only the marker counts
], ids=lambda shape: "-".join(f"{k}={v}" for k, v in shape.items()))
def test_abandon_refuses_any_other_markerless_shape(harness, capsys, shape):
    _rolled_back_switch(harness)
    before = _pre_marker_journal(harness, **shape)
    capsys.readouterr()
    with pytest.raises(spa.Refused, match="no verified restore"):
        harness.controller().abandon()
    assert harness.calls == [] and harness.journal() == before
    assert PRE_MARKER_MESSAGE not in capsys.readouterr().out


def test_abandon_of_a_pre_marker_journal_refuses_when_the_last_stage_is_not_its_restore(harness):
    _rolled_back_switch(harness)
    before = _pre_marker_journal(harness)
    record = pg.load_stage_record(harness.env_dir)
    pg.write_json(pg.stage_record_path(harness.env_dir), record | {"stage": "switch"})
    with pytest.raises(spa.Refused, match="last applied stage is switch"):
        harness.controller().abandon()
    assert harness.calls == [] and harness.journal() == before


def test_abandon_of_a_pre_marker_journal_refuses_when_the_live_check_cannot_run(harness):
    old, _ = _rolled_back_switch(harness)
    before = _pre_marker_journal(harness)
    real_run = harness.run

    def unreadable(argv, env=None, check=False):
        if argv[0] == "inventory":
            harness.calls.append("inventory:unreadable")
            return SimpleNamespace(returncode=2)
        return real_run(argv, env=env, check=check)

    harness.run = unreadable
    with pytest.raises(spa.Refused, match="live check couldn't run"):
        harness.controller().abandon()
    assert harness.calls == ["abandon:plan", "inventory:unreadable"] and harness.journal() == before
    assert live_tags(live_state(harness.env_dir)) == old["tags"]


def test_abandon_of_a_rolled_back_switch_refuses_when_a_new_column_keeps_its_new_mask(harness):
    old = released(harness)
    failed = write_env(harness.env_dir, columns=("email", "contact"))
    assert failed["tags"].keys() - old["tags"].keys()
    harness.fail_verify = {"switch"}
    assert harness.apply() == 1
    t = harness.journal()["transition"]
    assert t["restored"] and t["restored"]["tags"] != old["tags"]
    harness.calls.clear()
    with pytest.raises(spa.Refused, match="keep the new generation's treatment"):
        harness.controller().abandon()
    assert harness.calls == []


def test_a_resumed_switch_clears_the_restored_marker(harness):
    _, failed = _rolled_back_switch(harness)
    harness.crash_after = "switch"
    with pytest.raises(KeyboardInterrupt):
        harness.apply()
    t = harness.journal()["transition"]
    assert t["switch_attempted"] and t["restored"] is None
    with pytest.raises(spa.Refused, match="no verified restore"):
        harness.controller().abandon()


def test_failed_retire_keeps_journal_and_state_consistent_and_resumes(harness):
    old = released(harness)
    new = write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}})
    assert harness.apply() == 0 and harness.controller().confirm() == 0
    assert harness.apply(FAKE_TF_FAIL_APPLY="retire") == 1
    journal = json.loads(spa.journal_path(harness.env_dir).read_text())  # still valid JSON
    assert journal["transition"]["failed_phase"] == "retire" and "restore" not in harness.calls
    assert journal["active"] == new["fingerprint"]
    assert journal["pending"][0]["fingerprint"] == old["fingerprint"]
    # Columns keep the new protection throughout.
    assert live_tags(live_state(harness.env_dir)) == new["tags"]
    harness.calls.clear()
    assert harness.apply() == 0
    assert harness.calls[0] == "retire"
    assert harness.journal()["pending"] == []
    assert {p["name"] for p in live_policies(live_state(harness.env_dir))} == {p["name"] for p in new["fgac_policies"]}


def test_failed_function_drop_resumes_without_retiring_twice(harness, monkeypatch):
    released(harness)
    write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}})
    assert harness.apply() == 0 and harness.controller().confirm() == 0
    real = harness._drop
    harness._drop = lambda argv, check=False: SimpleNamespace(returncode=1)
    assert harness.apply() == 1
    assert harness.journal()["transition"]["failed_phase"] == "drop-functions"
    harness._drop = real
    harness.calls.clear()
    assert harness.apply() == 0
    assert harness.calls == ["drop-functions", "grant"]


# -- tier moves -------------------------------------------------------------------------------
@pytest.mark.parametrize("before, after", [
    (["raw", "regional", "masked"], ["raw", "analysts", "masked"]),  # a group moves into the middle tier
    (["raw", "masked"], ["raw", "regional", "masked"]),               # a middle tier is added
    (["raw", "regional", "masked"], ["raw", "masked"]),               # the middle tier is removed
    (["raw", "regional", "masked"], ["regional", "raw", "masked"]),   # two groups swap tiers
])
def test_tier_move_tightens_before_it_loosens(harness, before, after):
    old = released(harness, tiers=before)
    new = write_env(harness.env_dir, tiers=after)
    assert old["tags"] == new["tags"]  # a tier move needs no new value
    assert harness.apply() == 0
    assert harness.calls[:4] == ["create", "inventory:create/restore", "verify-access:create", "tighten"]
    assert harness.calls[4:] == ["inventory:tighten", "switch", "inventory:switch", "verify-access:switch", "grant"]
    # verify-created checks the old tiers (what columns still give), verify-switched the new ones.
    assert harness.verify_envs[0]["access_tier_groups"] == before
    assert harness.verify_envs[1]["access_tier_groups"] == after
    assert simulated_verify_access(harness.env_dir, *_final_inputs(harness)) == []


def _final_inputs(harness):
    t = {"from": "", "to": harness.journal()["active"], "tier_move": False}
    return harness.controller().verification_inputs(t, ("switch",))


def test_failed_switch_of_a_tier_move_goes_back_through_tighten(harness):
    old = released(harness)
    write_env(harness.env_dir, tiers=["raw", "analysts", "masked"])
    harness.fail_verify = {"switch"}
    assert harness.apply() == 1
    assert harness.calls[-3:] == ["tighten", "restore", "inventory:restore"]
    live = {p["name"]: p for p in live_policies(live_state(harness.env_dir))}
    assert {n: (p["to_principals"], p["except_principals"]) for n, p in live.items()} == {
        p["name"]: (p["to_principals"], p["except_principals"]) for p in old["fgac_policies"]}


def test_tier_move_and_treatment_change_together(harness):
    released(harness)
    new = write_env(harness.env_dir, tiers=["raw", "analysts", "masked"], versions={"email_partial": {"partial": "redacted"}},
                    columns=("email", "phone"))
    assert harness.apply() == 0
    assert live_tags(live_state(harness.env_dir)) == new["tags"]


def test_ungoverning_a_column_untags_it_only_at_switch(harness):
    released(harness, columns=("email", "phone"))
    new = write_env(harness.env_dir, columns=("email",))
    assert harness.apply() == 0
    assert live_tags(live_state(harness.env_dir)) == new["tags"]


# -- refusals before anything is applied --------------------------------------------------------
def test_unreadable_journal_is_refused(harness):
    released(harness)
    spa.journal_path(harness.env_dir).write_text("{not json")
    with pytest.raises(spa.Refused, match="unreadable"):
        harness.apply()


def test_state_changed_outside_the_controller_is_refused(harness):
    released(harness)
    state = live_state(harness.env_dir)
    for resource in state["resources"]:
        if resource["name"] == "treatment":
            resource["instances"][0]["attributes"]["tag_value"] = "email_address__string__deadbeef"
    (harness.env_dir / "data_access" / "terraform.tfstate").write_text(json.dumps(state))
    write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}})
    harness.calls.clear()
    with pytest.raises(spa.Refused, match="doesn't match the policy journal"):
        harness.apply()
    assert harness.calls == []


def test_lost_journal_adopts_only_a_state_that_matches_the_config(harness):
    gen = released(harness)
    spa.journal_path(harness.env_dir).unlink()
    assert harness.apply() == 0
    assert harness.journal()["active"] == gen["fingerprint"]
    spa.journal_path(harness.env_dir).unlink()
    write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}})
    with pytest.raises(spa.Refused, match="no policy\njournal records|no policy journal records"):
        harness.apply()


def test_lock_held_by_another_run_is_refused(harness):
    write_env(harness.env_dir)
    other = subprocess.Popen(["sleep", "30"])
    try:
        ok, _ = environment_lock.acquire_lock(harness.env_dir, other.pid, "release")
        assert ok
        with pytest.raises(spa.Refused, match="held by make release"):
            harness.apply()
        assert harness.calls == []
    finally:
        other.kill()
        other.wait()


def test_lock_held_by_the_enclosing_make_is_reused(harness, monkeypatch):
    write_env(harness.env_dir)
    ok, _, nonce = environment_lock.acquire(harness.env_dir, os.getpid(), "release")
    assert ok
    # The enclosing make exports the lock's nonce to its children (_ENV_LOCK).
    monkeypatch.setenv(environment_lock.nonce_var(harness.env_dir), nonce)
    assert harness.apply() == 0
    assert environment_lock.owns_lock(harness.env_dir, os.getpid())  # left for its owner to release


def test_confirm_refuses_while_unfinished_or_off_the_generated_config(harness):
    released(harness)
    write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}})
    assert harness.controller().confirm() == 1
    assert harness.apply(FAKE_TF_FAIL_APPLY="switch") == 1
    assert harness.controller().confirm() == 1


def test_verification_env_file_round_trips_through_hcl(tmp_path):
    value = {"a": ["b", "c"], "d e": {"partial": "x"}, "f": True, "g": None, "h": 1.5}
    path = tmp_path / "x.tfvars"
    path.write_text("".join(f"{k} = {spa._hcl_value(v)}\n" for k, v in {"v": value, "s": 'q"t'}.items()))
    assert hcl2.loads(path.read_text()) == {"v": value, "s": 'q"t'}


def test_status_and_describe_print_the_journal(harness, capsys):
    released(harness)
    write_env(harness.env_dir, tiers=["raw", "analysts", "masked"])
    assert harness.controller().describe() == 0
    assert "create -> verify-created -> tighten -> verify-tightened -> switch -> verify-switched" in capsys.readouterr().out
    assert harness.apply(FAKE_TF_FAIL_APPLY="tighten") == 1
    capsys.readouterr()
    assert spa.main(["status", "--env-dir", str(harness.env_dir), "--commit", "x"]) == 0
    out = capsys.readouterr().out
    assert "Unfinished policy change" in out and "failed at tighten" in out


def test_describe_never_fails_a_plan(harness, capsys):
    gen = write_env(harness.env_dir)
    spa.journal_path(harness.env_dir).parent.mkdir(parents=True, exist_ok=True)
    spa.journal_path(harness.env_dir).write_text("{not json")
    assert harness.controller().describe() == 0
    assert "no phase preview" in capsys.readouterr().out
    assert gen and harness.calls == []


def test_new_access_opens_only_in_the_last_phase_after_every_verification(harness, tmp_path):
    """table_readers SELECT (step 8): every staged plan withholds new grants; only grant allows them."""
    released(harness)
    write_env(harness.env_dir, tiers=["raw", "analysts", "masked"], versions={"email_partial": {"partial": "redacted"}})
    assert harness.apply() == 0 and harness.controller().confirm() == 0
    write_env(harness.env_dir, tiers=["raw", "analysts", "masked"], versions={"email_partial": {"partial": "initials"}})
    log = tmp_path / "envs" / "dev" / "runner.log"
    log.write_text("")
    assert harness.apply() == 0
    phases = harness.journal()["history"][-1]["phases"]
    assert phases[-1] == "grant" and "retire" in phases and "verify-switched" in phases
    stages = [line.split("-var=policy_stage=")[1].split()[0] for line in log.read_text().splitlines()
              if line.startswith("plan ")]
    # One plan per applying phase, grant strictly last; each other stage withholds new SELECT in Terraform.
    assert stages[-1] == "grant" and "grant" not in stages[:-1]
    assert set(stages[:-1]) <= {"create", "tighten", "switch", "retire"}


def test_failed_grant_keeps_the_verified_protection_and_resumes_at_grant(harness):
    old = released(harness)
    new = write_env(harness.env_dir, versions={"email_partial": {"partial": "redacted"}})
    assert harness.apply(FAKE_TF_FAIL_PLAN="grant") == 1
    t = harness.journal()["transition"]
    assert t["failed_phase"] == "grant" and "restore" not in harness.calls
    assert live_tags(live_state(harness.env_dir)) == new["tags"]  # switched and verified; not undone
    harness.calls.clear()
    assert harness.apply() == 0
    assert harness.calls == ["grant"]
    assert harness.journal()["pending"][0]["fingerprint"] == old["fingerprint"]
