"""Migration of a legacy deployment to deterministic governance (design section 8).

A legacy deployment (legacy gr_mask_<catalog>_<value> policies, value-keyed
gr_treatment tags, SELECT and CAN_RUN grants) sits in a fake Terraform state.
The env opts in with a committed deterministic config; the real staged policy
controller then adopts the legacy deployment through scripts/migration.py and
runs the phases against the fake layer of test_staged_policy_apply. After every
hook every governed column is checked: never unmasked for a principal both the
legacy and the deterministic protection mask, never doubly masked.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import hcl2
import pytest

from scripts import migration
from scripts import policy_generations as pg
from scripts import release_gate as rg
from scripts import staged_policy_apply as spa
from scripts.external_mask_inventory import external_masks
from ack_file import AckFileError, parse_line
from tests.test_staged_policy_apply import Harness, _covers, _value_of, live_policies, live_state, write_env

SHARED = Path(__file__).resolve().parents[1]
ENV = "dev"
COLUMN = "cat.sch.t.email"
LEGACY_VALUE = "email_partial"
LEGACY_POLICY = f"gr_mask_cat_{LEGACY_VALUE}"
TAG_KEY = f"columns|{COLUMN}|gr_treatment|{LEGACY_VALUE}"

STATE_TOOL = r'''#!/usr/bin/env python3
"""terraform_layer.sh state-mv / state-rm on the fake JSON state; anything else goes to the fake runner."""
import json, os, re, sys
layer, env_name, command, *args = sys.argv[1:]
if command not in ("state-mv", "state-rm"):
    os.execv(sys.executable, [sys.executable, os.environ["FAKE_INNER_RUNNER"], *sys.argv[1:]])
path = os.path.join(os.environ["LAYER_ENV_DIR"], "terraform.tfstate")
with open(os.path.join(os.environ["LAYER_ENV_DIR"], "..", "state_tool.log"), "a") as log:
    log.write(" ".join([command] + args) + "\n")
state = json.load(open(path))
ADDR = re.compile(r'module\.data_access\.(\w+)\.(\w+)\["(.*)"\]$')


def resource(rtype, name):
    for r in state["resources"]:
        if (r["type"], r["name"]) == (rtype, name):
            return r
    r = {"module": "module.data_access", "mode": "managed", "type": rtype, "name": name, "instances": []}
    state["resources"].append(r)
    return r


rtype, name, key = ADDR.match(args[0]).groups()
source = resource(rtype, name)
moved = [i for i in source["instances"] if i["index_key"] == key]
if not moved:
    sys.exit(f"no instance {args[0]}")
source["instances"] = [i for i in source["instances"] if i["index_key"] != key]
if command == "state-mv":
    _, target_name, target_key = ADDR.match(args[1]).groups()
    resource(rtype, target_name)["instances"].append(dict(moved[0], index_key=target_key))
json.dump(state, open(path, "w"), indent=1, sort_keys=True)
'''


# -- a legacy deployment ---------------------------------------------------------
def _resource(rtype, name, instances, module="module.data_access"):
    return {"module": module, "mode": "managed", "type": rtype, "name": name,
            "instances": [{"index_key": k, "attributes": a} for k, a in instances.items()]}


def legacy_policy(to=("regional", "masked"), value=LEGACY_VALUE, catalog="cat"):
    return {"catalog": catalog, "policy_type": "POLICY_TYPE_COLUMN_MASK", "to_principals": list(to),
            "except_principals": [], "column_mask": {"function_name": f"{catalog}.sch.mask_email"},
            "match_columns": [{"condition": f"hasTagValue('gr_treatment', '{value}')"}]}


def write_legacy_state(env_dir: Path, *, policies=None, grants=("regional", "masked"), can_run=("regional",),
                       extra=(), columns=(COLUMN,)):
    """What legacy GenieRails left in the data_access and workspace states."""
    policies = {LEGACY_POLICY: legacy_policy()} if policies is None else policies
    tags = {f"columns|{c}|gr_treatment|{LEGACY_VALUE}": {"entity_name": c, "tag_value": LEGACY_VALUE}
            for c in columns}
    resources = [
        _resource("databricks_policy_info", "policies", policies),
        _resource("databricks_entity_tag_assignment", "assignments", tags),
        _resource("databricks_grant", "table_access",
                  {f"cat.sch.t|{g}": {"table": "cat.sch.t", "principal": g} for g in grants}),
        _resource("databricks_grant", "catalog_access", {f"cat|{g}": {} for g in grants}),
        *extra,
    ]
    layer = env_dir / "data_access"
    layer.mkdir(parents=True, exist_ok=True)
    (layer / "terraform.tfstate").write_text(json.dumps(
        {"version": 4, "serial": 3, "lineage": "L", "resources": resources, "functions": ["cat.sch.mask_email"]}))
    (env_dir / "terraform.tfstate").write_text(json.dumps({"version": 4, "resources": [_resource(
        "null_resource", "genie_space_acls",
        {"sales": {"triggers": {"space_id": "s1", "groups": ",".join(can_run)}}}, module="module.workspace")]}))


def opt_in(env_dir: Path, *, readers=("regional", "masked"), acl_groups=("regional",), ack=(), tiers=None,
           columns=("email",)):
    """The committed opt-in: deterministic generated config, table_readers, the agent and ack.txt."""
    write_env(env_dir, columns=columns, **({"tiers": tiers} if tiers else {}))
    with (env_dir / "env.auto.tfvars").open("a") as f:
        f.write(f'table_readers = {{ "cat.sch.t" = {json.dumps(list(readers))} }}\n')
        acl = "" if acl_groups is None else f", acl_groups = {json.dumps(list(acl_groups))}"
        f.write(f'genie_spaces = [{{ name = "Sales", genie_space_id = "s1", uc_tables = ["cat.sch.t"]{acl} }}]\n')
    (env_dir / "ack.txt").write_text("".join(f"{line}\n" for line in ack))


@pytest.fixture
def env_dir(tmp_path):
    path = tmp_path / "envs" / ENV
    path.mkdir(parents=True)
    write_legacy_state(path)
    return path


# -- the cutover harness -----------------------------------------------------------
class MigrationHarness(Harness):
    """The staged-policy harness, with state moves and a migration invariant instead of the tier one."""

    def __init__(self, env_dir: Path, monkeypatch, tmp_path: Path):
        super().__init__(env_dir, monkeypatch, tmp_path)
        tool = tmp_path / "fake_state_tool.py"
        tool.write_text(STATE_TOOL)
        tool.chmod(0o755)
        monkeypatch.setenv("FAKE_INNER_RUNNER", str(pg.RUNNER))
        monkeypatch.setattr(pg, "RUNNER", tool)
        self.simulate_access = False  # legacy columns are checked by the legacy verify-access path
        self.legacy = None
        self.desired = None

    def _check_invariants(self, stage, before, env):
        state = live_state(self.env_dir)
        legacy = self.legacy or pg.load_snapshot(self.env_dir, env.get("GENIERAILS_OLD_POLICY_FINGERPRINT", ""))
        desired = pg.desired_generation(self.env_dir)
        tags = {**{a["entity_name"]: a["tag_value"] for a in _attrs(state, "assignments")},
                **{a["entity_name"]: a["tag_value"] for a in _attrs(state, "treatment")}}
        if COLUMN not in tags:
            self.invariant_problems.append(f"after {stage}: {COLUMN} lost its treatment tag")
        universe = pg._universe(legacy, desired) | {pg.OUTSIDER}
        for column, value in tags.items():
            masks = [p for p in live_policies(state) if _value_of(p) == value and p["catalog"] == column.split(".")[0]]
            for who in universe:
                applied = [p for p in masks if _covers(p, who)]
                was = any(_covers_def(p, who) for p in (legacy or {}).get("fgac_policies") or []
                          if pg.policy_value(p) == (legacy or {}).get("tags", {}).get(column))
                will = any(_covers_def(p, who) for p in desired["fgac_policies"]
                           if pg.policy_value(p) == desired["tags"].get(column))
                if len(applied) > 1:
                    self.invariant_problems.append(f"after {stage}: {who} has {len(applied)} masks on {column}")
                if was and will and not applied:
                    self.invariant_problems.append(f"after {stage}: {who} sees {column} raw (mask gap)")


def _attrs(state, name):
    for resource in state["resources"]:
        if (resource["type"], resource["name"]) == ("databricks_entity_tag_assignment", name):
            return [i["attributes"] for i in resource["instances"]]
    return []


def _covers_def(policy, who):
    return _covers({"to_principals": policy.get("to_principals") or [],
                    "except_principals": policy.get("except_principals") or []}, who)


@pytest.fixture
def cutover(env_dir, monkeypatch, tmp_path):
    h = MigrationHarness(env_dir, monkeypatch, tmp_path)
    yield h
    assert not h.invariant_problems, h.invariant_problems


def _snapshot_files(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}


# -- legacy envs that never opt in -------------------------------------------------
def test_an_env_that_never_opts_in_is_untouched(env_dir, capsys):
    (env_dir / "env.auto.tfvars").write_text('genie_space_id = "s1"\n')  # governance_mode unset: legacy
    before = _snapshot_files(env_dir)
    calls = []
    controller = spa.Controller(env_dir, ENV, run=lambda *a, **k: calls.append(a))
    for command in ("run", "plan", "describe"):
        assert (controller.plan(env_dir / "plans") if command == "plan" else getattr(controller, command)()) == 0
    assert calls == [] and spa.load_journal(env_dir) is None
    assert not migration.pending(env_dir) and migration.baseline(env_dir) is None
    assert migration.accounted(env_dir, ENV) == ({}, [])
    rg.apply_guard(env_dir, ENV, "_apply-layer:data_access")  # legacy applies are never gated
    assert _snapshot_files(env_dir) == before


def test_the_dry_run_before_opting_in_proposes_table_readers_from_todays_grants(env_dir, tmp_path):
    (env_dir / "env.auto.tfvars").write_text('access_tier_groups = ["raw", "regional"]\n')
    write_legacy_state(env_dir, grants=("regional", "masked", "bi-sp"))
    lines, data = migration.report(env_dir, ENV)
    assert data["proposed_table_readers"] == {"cat.sch.t": ["bi-sp", "masked", "regional"]}
    text = "\n".join(lines)
    assert '"cat.sch.t" = ["bi-sp", "masked", "regional"]' in text
    assert "can't be readers: bi-sp, masked" in text and 'set governance_mode = "deterministic"' in text
    # The first deterministic generate refuses until the governed-table record exists.
    rebuild = text.index(f"make rebuild-governed-tables ENV={ENV}")
    assert rebuild < text.index("then make generate")
    assert not data["ok"]


# -- the dry run ---------------------------------------------------------------------
def test_the_dry_run_reports_every_column_principal_revocation_and_writes_nothing(env_dir, tmp_path):
    write_legacy_state(env_dir, grants=("regional", "masked", "bi-sp"), can_run=("regional", "masked"))
    opt_in(env_dir)
    (env_dir / "data_access" / "masking_functions.sql").write_text(
        "USE CATALOG cat;\nUSE SCHEMA sch;\nCREATE OR REPLACE FUNCTION mask_email(email STRING)\n"
        "RETURNS STRING\nRETURN CONCAT('***@', SPLIT(email, '@')[1]);\n"
        + (env_dir / "data_access" / "masking_functions.sql").read_text())
    before = _snapshot_files(env_dir)
    out = tmp_path / "out"
    result = subprocess.run([sys.executable, str(SHARED / "scripts" / "migration.py"), "report", "--env-dir",
                             str(env_dir), "--env", ENV, "--out-dir", str(out)], text=True, capture_output=True)
    assert result.returncode == 1, result.stdout + result.stderr
    text = result.stdout
    assert _snapshot_files(env_dir) == before  # read-only
    # Column by column, principal by principal; old function against the library's.
    assert f"{COLUMN}: {LEGACY_VALUE} -> email_partial__string__" in text
    assert "mask today: mask_email: CONCAT('***@', SPLIT(email, '@')[1])" in text
    assert re.search(r"after \(partial\): gr_mask_email_partial__string__\w+_partial", text)
    assert re.search(r"after \(full\): gr_mask_email_partial__string__\w+_full", text)
    assert "    masked: partial (email_partial) -> redacted (email_partial)   <- changes" in text
    assert "    regional: partial (email_partial) -> partial (email_partial)\n" in text
    assert "    raw: raw -> raw\n" in text
    # Policy principals under the tier rule.
    assert f"legacy {LEGACY_POLICY} ({LEGACY_VALUE}): TO ['regional', 'masked'] EXCEPT []" in text
    assert re.search(r"new    gr_mask_cat_email_partial__string__\w+_partial: TO \['regional'\]", text)
    # Every SELECT and CAN_RUN revoked, the proposal, the tag move.
    assert "NEEDS ACK    dev:revoke:cat.sch.t:bi-sp: bi-sp loses SELECT on cat.sch.t" in text
    assert "NEEDS ACK    dev:revoke:sales:masked: masked loses CAN_RUN on agent sales" in text
    assert "Also removed (no data by themselves): catalog_access:cat|bi-sp" in text
    assert f"terraform state mv 'module.data_access.databricks_entity_tag_assignment.assignments[\"{TAG_KEY}\"]' " \
           f"'module.data_access.databricks_entity_tag_assignment.treatment[\"columns|{COLUMN}|gr_treatment\"]'" in text
    report = json.loads((out / "report.json").read_text())
    assert report["proposed_table_readers"] == {"cat.sch.t": ["bi-sp", "masked", "regional"]}
    assert sorted(report["unacknowledged_revocations"]) == ["dev:revoke:cat.sch.t:bi-sp", "dev:revoke:sales:masked"]
    assert '"cat.sch.t" = ["bi-sp", "masked", "regional"]' in (out / "table_readers.proposed.hcl").read_text()


def test_the_dry_run_passes_once_every_revocation_is_acknowledged(env_dir):
    write_legacy_state(env_dir, grants=("regional", "masked", "bi-sp"))
    opt_in(env_dir, ack=["dev:revoke:cat.sch.t:bi-sp"])
    lines, data = migration.report(env_dir, ENV)
    assert data["ok"], "\n".join(lines)
    assert "ACKNOWLEDGED dev:revoke:cat.sch.t:bi-sp" in "\n".join(lines)


def test_the_dry_run_refuses_a_stale_acknowledgement(env_dir):
    opt_in(env_dir, ack=["dev:revoke:cat.sch.t:nobody"])
    _, data = migration.report(env_dir, ENV)
    assert not data["ok"] and data["stale_acknowledgements"] == ["dev:revoke:cat.sch.t:nobody"]


# -- refusals -----------------------------------------------------------------------
def _refusal(env_dir) -> str:
    with pytest.raises(migration.Refused) as exc:
        migration.check(env_dir, ENV, migration.legacy_generation(migration.deployment(env_dir)))
    return str(exc.value)


def test_a_revoked_select_is_refused_until_acknowledged(env_dir):
    write_legacy_state(env_dir, grants=("regional", "masked", "bi-sp"))
    opt_in(env_dir)
    assert "unacknowledged revocation dev:revoke:cat.sch.t:bi-sp" in _refusal(env_dir)
    opt_in(env_dir, ack=["dev:revoke:cat.sch.t:bi-sp"])
    assert migration.check(env_dir, ENV, migration.legacy_generation(migration.deployment(env_dir)))["ok"]


def test_a_revoked_can_run_is_refused_until_acknowledged(env_dir):
    write_legacy_state(env_dir, can_run=("regional", "masked"))
    opt_in(env_dir)
    assert "unacknowledged revocation dev:revoke:sales:masked: masked loses CAN_RUN" in _refusal(env_dir)
    opt_in(env_dir, ack=["dev:revoke:sales:masked"])
    migration.check(env_dir, ENV, migration.legacy_generation(migration.deployment(env_dir)))


def test_a_weakened_column_is_refused_until_acknowledged(env_dir):
    # Legacy masked everyone (the fallback mask); deterministic tier 1 sees raw.
    write_legacy_state(env_dir, policies={LEGACY_POLICY: legacy_policy(to=["account users"])})
    opt_in(env_dir)
    assert f"unacknowledged weakening dev:weaken:{COLUMN}:raw: partial ({LEGACY_VALUE}) -> raw" in _refusal(env_dir)
    opt_in(env_dir, ack=[f"dev:weaken:{COLUMN}:raw"])
    migration.check(env_dir, ENV, migration.legacy_generation(migration.deployment(env_dir)))


def test_a_legacy_redaction_turned_partial_is_a_weakening(env_dir):
    redact = legacy_policy(value="redact") | {"to_principals": ["regional", "masked"]}
    state = json.loads((env_dir / "data_access" / "terraform.tfstate").read_text())
    state["resources"][0]["instances"] = [{"index_key": "gr_mask_cat_redact", "attributes": redact}]
    state["resources"][1]["instances"] = [{"index_key": f"columns|{COLUMN}|gr_treatment|redact",
                                           "attributes": {"entity_name": COLUMN, "tag_value": "redact"}}]
    (env_dir / "data_access" / "terraform.tfstate").write_text(json.dumps(state))
    opt_in(env_dir)
    assert f"dev:weaken:{COLUMN}:regional: redacted (redact) -> partial" in _refusal(env_dir)


def test_a_new_reader_is_widening(env_dir):
    opt_in(env_dir, readers=("regional", "masked", "raw"))
    assert "unacknowledged weakening dev:reader:cat.sch.t:raw" in _refusal(env_dir)


def test_an_agent_without_acl_groups_is_refused(env_dir):
    opt_in(env_dir, acl_groups=None)
    assert "agent sales has no acl_groups" in _refusal(env_dir)


def test_a_legacy_row_filter_stays_frozen(env_dir):
    row_filter = {"policy_type": "POLICY_TYPE_ROW_FILTER", "catalog": "cat", "to_principals": ["regional"],
                  "except_principals": [], "row_filter": {"function_name": "cat.sch.filter_region"},
                  "match_columns": [{"condition": "hasTagValue('region', 'apac')", "alias": "region"}]}
    write_legacy_state(env_dir, policies={LEGACY_POLICY: legacy_policy(), "filter_region": row_filter})
    opt_in(env_dir)
    assert "legacy POLICY_TYPE_ROW_FILTER filter_region is missing from the config" in _refusal(env_dir)
    frozen = migration.deployment(env_dir)["frozen"]["filter_region"]
    abac = env_dir / "generated" / "abac.auto.tfvars"
    cfg = hcl2.loads(abac.read_text())
    import generate_abac

    for policy in (frozen | {"to_principals": ["regional", "masked"]}, frozen):
        text = generate_abac._replace_bracket_section(abac.read_text(), "fgac_policies", [
            generate_abac._render_fgac_policy_block(p) for p in cfg["fgac_policies"] + [policy]])
        abac.write_text(text)
        if policy is not frozen:
            assert "legacy POLICY_TYPE_ROW_FILTER filter_region differs from what is deployed" in _refusal(env_dir)
    migration.check(env_dir, ENV, migration.legacy_generation(migration.deployment(env_dir)))
    assert "filter_region" in "\n".join(migration.report(env_dir, ENV)[0])


def test_a_legacy_mask_not_on_the_treatment_tag_is_refused(env_dir):
    odd = legacy_policy() | {"match_columns": [{"condition": "hasTagValue('pii_level', 'high')"}]}
    write_legacy_state(env_dir, policies={LEGACY_POLICY: legacy_policy(), "mask_pii": odd})
    opt_in(env_dir)
    assert "legacy column mask mask_pii doesn't match on gr_treatment" in _refusal(env_dir)


def test_a_legacy_generated_config_is_refused(env_dir):
    opt_in(env_dir)
    abac = env_dir / "generated" / "abac.auto.tfvars"
    abac.write_text(re.sub(r'tag_value\s*=\s*"[^"]+"', f'tag_value = "{LEGACY_VALUE}"', abac.read_text()))
    assert "the generated config is still legacy" in _refusal(env_dir)


# -- the cutover, phase by phase ---------------------------------------------------------
def test_the_cutover_stages_new_policies_first_and_keeps_legacy_until_a_later_release(cutover, env_dir):
    opt_in(env_dir)
    legacy = migration.legacy_generation(migration.deployment(env_dir))
    cutover.legacy = legacy
    assert cutover.apply() == 0
    log = (env_dir / "state_tool.log").read_text().splitlines()
    assert log == [f'state-mv module.data_access.databricks_entity_tag_assignment.assignments["{TAG_KEY}"] '
                   f'module.data_access.databricks_entity_tag_assignment.treatment["columns|{COLUMN}|gr_treatment"]']
    assert cutover.calls == ["create", "inventory:create/restore", "verify-access:create", "switch",
                             "inventory:switch", "verify-access:switch", "grant"]
    # Before the switch, columns still on the legacy value are checked by the legacy checks.
    assert cutover.verify_envs[0]["governance_mode"] == "legacy"
    assert cutover.verify_envs[1]["governance_mode"] == "deterministic"
    state = live_state(env_dir)
    names = {p["name"] for p in live_policies(state)}
    desired = pg.desired_generation(env_dir)
    assert LEGACY_POLICY in names and {p["name"] for p in desired["fgac_policies"]} <= names
    assert _attrs(state, "assignments") == [] and [a["tag_value"] for a in _attrs(state, "treatment")] \
        == [desired["tags"][COLUMN]]
    journal = cutover.journal()
    assert journal["active"] == desired["fingerprint"]
    assert journal["pending"] == [{"fingerprint": legacy["fingerprint"], "superseded_by": journal["history"][-1]
                                   ["run_id"], "confirmed": False}]
    record = migration.load_record(env_dir)
    assert record["legacy"] == legacy["fingerprint"] and record["moved"]

    # Not retired by the same release; the next successful one retires it, and no function is dropped.
    assert cutover.apply() == 0
    assert LEGACY_POLICY in {p["name"] for p in live_policies(live_state(env_dir))}
    assert cutover.controller().confirm() == 0
    cutover.calls.clear()
    assert cutover.apply() == 0
    assert "retire" in cutover.calls and "drop-functions" in cutover.calls and cutover.dropped == []
    assert LEGACY_POLICY not in {p["name"] for p in live_policies(live_state(env_dir))}


def test_a_refused_migration_changes_nothing(cutover, env_dir):
    write_legacy_state(env_dir, grants=("regional", "masked", "bi-sp"))
    opt_in(env_dir)
    before = live_state(env_dir)
    assert spa.main(["run", "--env-dir", str(env_dir), "--env-name", ENV]) == 3
    assert live_state(env_dir) == before and spa.load_journal(env_dir) is None
    assert migration.load_record(env_dir) is None and not (env_dir / "state_tool.log").exists()


def test_an_interrupted_adoption_resumes(cutover, env_dir, monkeypatch):
    opt_in(env_dir)
    real = migration._terraform_state

    def crash_after_moving(env_dir_, env_):
        run = real(env_dir_, env_)

        def once(command, *addresses):
            rc = run(command, *addresses)
            raise KeyboardInterrupt("crash after the state move")
        return once
    monkeypatch.setattr(migration, "_terraform_state", crash_after_moving)
    with pytest.raises(KeyboardInterrupt):
        cutover.apply()
    monkeypatch.setattr(migration, "_terraform_state", real)
    assert spa.load_journal(env_dir) is None and migration.pending(env_dir)
    assert migration.deployment(env_dir)["moves"] == []  # the tag already sits at its stable key
    cutover.legacy = migration.legacy_generation(migration.deployment(env_dir))
    assert cutover.apply() == 0
    assert spa.load_journal(env_dir)["active"] == pg.desired_generation(env_dir)["fingerprint"]


def test_saved_plans_take_one_manifest_per_changing_phase(cutover, env_dir, tmp_path):
    opt_in(env_dir)
    cutover.legacy = migration.legacy_generation(migration.deployment(env_dir))
    controller = cutover.controller()
    adopt = tmp_path / "plan0"
    assert controller.plan(adopt) == 0
    phases = json.loads((adopt / spa.PHASES_FILE).read_text())
    assert [s["phase"] for s in phases["steps"]] == ["adopt"] and not phases["final"]
    assert phases["steps"][0]["moves"] == [list(m) for m in migration.deployment(env_dir)["moves"]]
    assert controller.apply_saved(adopt) == spa.MORE_PHASES  # the approved moves, and nothing else
    assert migration.deployment(env_dir)["moves"] == [] and cutover.calls == []
    assert spa.load_journal(env_dir)["active"] == cutover.legacy["fingerprint"]
    first = tmp_path / "plan1"
    assert controller.plan(first) == 0
    phases = json.loads((first / spa.PHASES_FILE).read_text())
    assert [s["phase"] for s in phases["steps"]] == ["create", "verify-created"] and not phases["final"]
    assert phases["transition"]["from"] == cutover.legacy["fingerprint"]
    assert controller.apply_saved(first) == spa.MORE_PHASES
    second = tmp_path / "plan2"
    assert controller.plan(second) == 0
    assert [s["phase"] for s in json.loads((second / spa.PHASES_FILE).read_text())["steps"]] == \
        ["switch", "verify-switched"]
    assert controller.apply_saved(second) == spa.MORE_PHASES
    third = tmp_path / "plan3"
    assert controller.plan(third) == 0
    assert controller.apply_saved(third) == 0
    assert spa.load_journal(env_dir)["active"] == pg.desired_generation(env_dir)["fingerprint"]


def test_make_plan_previews_the_migration_without_moving_anything(env_dir, capsys):
    opt_in(env_dir)
    before = live_state(env_dir)
    assert spa.Controller(env_dir, ENV).describe() == 0
    out = capsys.readouterr().out
    assert "migrates from the legacy deployment" in out
    assert "would run: create -> verify-created -> switch -> verify-switched -> grant" in out
    assert live_state(env_dir) == before and spa.load_journal(env_dir) is None


# -- release-plan reports --------------------------------------------------------------
def _source():
    from reader_inventory import FakeSource

    # The deployer is tier 1 here, so the reader inventory finds no reader outside the tiers.
    return FakeSource(owners={("CATALOG", "cat"): "raw", ("SCHEMA", "cat.sch"): "raw",
                              ("TABLE", "cat.sch.t"): "raw"}, admins={"raw"}, deployer="raw")


def test_release_plan_compares_with_the_legacy_masks_and_enforces_revocations(env_dir):
    write_legacy_state(env_dir, policies={LEGACY_POLICY: legacy_policy(to=["account users"])},
                       grants=("regional", "masked", "bi-sp"))
    opt_in(env_dir)
    report = rg.reports(env_dir, ENV, reader_source=_source)
    assert not report["ok"]
    assert f"dev:weaken:{COLUMN}:raw" in {w["ack_key"] for w in report["weakening"]["unacknowledged"]}
    assert [r["ack_key"] for r in report["revocations"]["unacknowledged"]] == ["dev:revoke:cat.sch.t:bi-sp"]
    assert report["readers"]["tables"] == ["cat.sch.t"]  # a first deterministic apply: every table inventoried
    opt_in(env_dir, ack=[f"dev:weaken:{COLUMN}:raw"])  # the revocation alone still refuses
    report = rg.reports(env_dir, ENV, reader_source=_source)
    assert not report["ok"] and not report["weakening"]["unacknowledged"]
    opt_in(env_dir, ack=[f"dev:weaken:{COLUMN}:raw", "dev:revoke:cat.sch.t:bi-sp"])
    assert rg.reports(env_dir, ENV, reader_source=_source)["ok"]


def test_acknowledgements_stay_accounted_for_after_the_adoption(cutover, env_dir):
    write_legacy_state(env_dir, grants=("regional", "masked", "bi-sp"))
    opt_in(env_dir, ack=["dev:revoke:cat.sch.t:bi-sp"])
    cutover.legacy = migration.legacy_generation(migration.deployment(env_dir))
    assert cutover.apply() == 0
    report = rg.reports(env_dir, ENV, reader_source=_source)
    assert report["ok"] and report["unmatched_acknowledgements"] == []
    assert report["revocations"]["acknowledged"] == ["dev:revoke:cat.sch.t:bi-sp"]


def test_a_revoke_acknowledgement_outside_a_migration_is_stale(tmp_path):
    env_dir = tmp_path / "envs" / ENV
    opt_in(env_dir, ack=["dev:revoke:cat.sch.t:bi-sp"])
    report = rg.reports(env_dir, ENV, reader_source=_source)
    assert report["unmatched_acknowledgements"] == ["dev:revoke:cat.sch.t:bi-sp"] and not report["ok"]


# -- guards ---------------------------------------------------------------------------
def test_a_migrated_env_cannot_go_back_to_legacy(cutover, env_dir):
    opt_in(env_dir)
    cutover.legacy = migration.legacy_generation(migration.deployment(env_dir))
    assert cutover.apply() == 0
    env_file = env_dir / "env.auto.tfvars"
    env_file.write_text(env_file.read_text().replace('"deterministic"', '"legacy"'))
    for target in ("_apply-layer:data_access", "_apply-layer:workspace", "apply", "apply-governance",
                   "apply-genie"):
        with pytest.raises(rg.Refused, match="was migrated to deterministic governance"):
            rg.apply_guard(env_dir, ENV, target)


# -- release-plan never changes anything (review blocker 1) ----------------------------------
def _runtime_bytes(env_dir: Path) -> dict[str, bytes]:
    """Terraform state, the policy journal, the migration record and every ledger file."""
    files = [env_dir / "data_access" / "terraform.tfstate", env_dir / "terraform.tfstate"]
    files += sorted(pg.ledger_dir(env_dir).rglob("*")) if pg.ledger_dir(env_dir).is_dir() else []
    return {str(f.relative_to(env_dir)): f.read_bytes() for f in files if f.is_file()}


def test_release_plan_of_a_migration_changes_no_state_journal_or_record(cutover, env_dir, tmp_path):
    opt_in(env_dir)
    before = _runtime_bytes(env_dir)
    assert cutover.controller().plan(tmp_path / "plan") == 0  # what make release-plan runs
    assert _runtime_bytes(env_dir) == before
    assert not (env_dir / "state_tool.log").exists() and cutover.calls == []
    assert spa.load_journal(env_dir) is None and migration.load_record(env_dir) is None
    step = json.loads((tmp_path / "plan" / spa.PHASES_FILE).read_text())["steps"][0]
    assert step["kind"] == "adopt" and step["moves"][0][0].endswith(f'assignments["{TAG_KEY}"]')
    # A never-approved manifest is just dropped; the next release-plan plans the same moves.
    assert cutover.controller().plan(tmp_path / "again") == 0
    assert _runtime_bytes(env_dir) == before


def test_a_refused_release_plan_of_a_migration_changes_nothing(cutover, env_dir, tmp_path):
    write_legacy_state(env_dir, grants=("regional", "masked", "bi-sp"))
    opt_in(env_dir)
    before = _runtime_bytes(env_dir)
    with pytest.raises(spa.Refused, match="unacknowledged revocation"):
        cutover.controller().plan(tmp_path / "plan")
    assert _runtime_bytes(env_dir) == before and not (env_dir / "state_tool.log").exists()


def test_the_adoption_manifest_refuses_a_state_that_moved_since_release_plan(cutover, env_dir, tmp_path):
    opt_in(env_dir)
    assert cutover.controller().plan(tmp_path / "plan") == 0
    state = live_state(env_dir)
    state["resources"][1]["instances"][0]["index_key"] += "-changed"
    (env_dir / "data_access" / "terraform.tfstate").write_text(json.dumps(state))
    before = _runtime_bytes(env_dir)
    with pytest.raises(spa.Refused, match="the data_access state changed since release-plan"):
        cutover.controller().apply_saved(tmp_path / "plan")
    assert _runtime_bytes(env_dir) == before


# -- no way back, even mid-adoption (review blocker 2) -------------------------------------------
def _crash_after_one_move(env_dir: Path, monkeypatch):
    """Adopt a two-column legacy env and crash right after the first terraform state mv."""
    other = "cat.sch.t.phone"
    write_legacy_state(env_dir, columns=(COLUMN, other))
    opt_in(env_dir, columns=("email", "phone"))
    tool = Path(pg.RUNNER)
    calls = []

    def terraform(command, *addresses):
        calls.append(command)
        rc = subprocess.run([str(tool), "data_access", ENV, command, *addresses], check=False,
                            env=dict(os.environ, LAYER_ENV_DIR=str(env_dir / "data_access"))).returncode
        raise KeyboardInterrupt("crash after the first state move")
    with pytest.raises(KeyboardInterrupt):
        migration.adopt(env_dir, ENV, terraform=terraform)
    assert calls == ["state-mv"]
    state = live_state(env_dir)
    assert len(_attrs(state, "treatment")) == 1 and len(_attrs(state, "assignments")) == 1
    assert migration.load_record(env_dir)["status"] == "adopting"  # written before the move


def test_an_interrupted_adoption_already_refuses_legacy_applies(cutover, env_dir, monkeypatch):
    _crash_after_one_move(env_dir, monkeypatch)
    env_file = env_dir / "env.auto.tfvars"
    env_file.write_text(env_file.read_text().replace('"deterministic"', '"legacy"'))
    for target in ("_apply-layer:data_access", "apply", "apply-governance"):
        with pytest.raises(rg.Refused, match="was migrated to deterministic governance"):
            rg.apply_guard(env_dir, ENV, target)
    # Even without the marker, the state itself says a migration began.
    migration.record_path(env_dir).unlink()
    with pytest.raises(rg.Refused, match="treatment tags at stable keys"):
        rg.legacy_guard(env_dir, ENV, "apply")
    # Resuming in deterministic mode finishes the adoption.
    env_file.write_text(env_file.read_text().replace('"legacy"', '"deterministic"'))
    assert migration.pending(env_dir) and len(migration.deployment(env_dir)["moves"]) == 1


@pytest.mark.parametrize("target", ["apply", "apply-governance", "rehearse", "maintain", "release"])
@pytest.mark.parametrize("marker", [True, False])
def test_every_legacy_apply_path_refuses_after_an_interrupted_adoption(cutover, tmp_path, monkeypatch,
                                                                        side_door_envs, target, marker):
    from tests.test_release_no_side_door import LEGACY, applied, run

    cloud, _ = side_door_envs
    legacy_dir = cloud / "envs" / "legacy"
    cutover.env_dir = legacy_dir
    for name in ("generated", "data_access"):
        shutil.rmtree(legacy_dir / name)
    _crash_after_one_move(legacy_dir, monkeypatch)
    if not marker:
        migration.record_path(legacy_dir).unlink()
    (legacy_dir / "env.auto.tfvars").write_text(LEGACY)  # governance_mode back to legacy
    result, calls = run(tmp_path, side_door_envs, target, "legacy")
    assert result.returncode != 0, result.stdout + result.stderr
    assert f"make {target} refused: ENV=legacy was migrated to deterministic governance" in result.stderr
    assert applied(calls) == [], calls


def test_the_external_mask_inventory_knows_the_legacy_masks_it_manages(env_dir):
    from scripts.external_mask_inventory import legacy_policy_names

    own = legacy_policy_names(env_dir)
    assert own == {f"cat_{LEGACY_POLICY}"}
    live = {"cat.sch.t": [{"name": f"cat_{LEGACY_POLICY}", "policy_type": "COLUMN_MASK",
                           "function_name": "cat.sch.mask_email"},
                          {"name": "cat_hand_made", "policy_type": "COLUMN_MASK", "function_name": "cat.sch.f"}]}
    assert external_masks({COLUMN}, [], live, own) == [
        "cat.sch.t: column-mask policy 'cat_hand_made' on cat.sch.t (function cat.sch.f)"]
    assert len(external_masks({COLUMN}, [], live)) == 2


def test_revoke_acknowledgements_name_a_table_or_an_agent():
    assert parse_line("prod:revoke:cat.sch.t:bi-sp", expected_env="prod").object_name == "cat.sch.t"
    assert parse_line("prod:revoke:sales:analysts", expected_env="prod").principal == "analysts"
    with pytest.raises(AckFileError):
        parse_line("prod:revoke:cat.sch:analysts", expected_env="prod")


def test_the_same_value_in_two_catalogs_is_not_a_double_mask():
    def policy(catalog, name=None):
        return {"name": name or f"gr_mask_{catalog}_v", "catalog": catalog, "to_principals": ["account users"],
                "except_principals": [], "match_condition": "hasTagValue('gr_treatment', 'v')"}
    two = {"fgac_policies": [policy("a"), policy("b")], "tags": {"a.s.t.c": "v", "b.s.t.c": "v"}}
    assert pg.stage_problems("create", two, two, None) == []
    other = {"fgac_policies": [policy("b")], "tags": {"a.s.t.c": "v"}}
    assert pg.stage_problems("create", other, other, None) == ["a.s.t.c would carry v, which no policy masks"]
    double = {"fgac_policies": [policy("a"), policy("a", "x")], "tags": {"a.s.t.c": "v"}}
    assert "2 masks" in pg.stage_problems("create", double, double, None)[0]


@pytest.fixture
def side_door_envs(tmp_path):
    from tests.test_release_no_side_door import envs

    return envs.__wrapped__(tmp_path) if hasattr(envs, "__wrapped__") else envs(tmp_path)
