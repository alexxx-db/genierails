"""Rollout step 8: SELECT only from table_readers, CAN_RUN only from acl_groups,
the live agent coverage check, and group_map (decision 8).

Terraform-level proofs live in modules/data_access/tests/table_readers.tftest.hcl
and roots/workspace/tests/agent_coverage_gate.tftest.hcl (run by
test_coverage_gate_script.py::test_terraform_test_suites_pass); the mutation
tests at the end of this file prove those fail when a protection is reverted.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import hcl2
import pytest

SHARED = Path(__file__).resolve().parents[1]
ROOT = SHARED.parent
sys.path.insert(0, str(SHARED))

import table_readers as tr  # noqa: E402
from deterministic_governance import validate_config  # noqa: E402
from scripts import agent_coverage as ac  # noqa: E402
from scripts import release_helpers  # noqa: E402

TIERS = ["raw", "partial", "full"]
needs_make = pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")


def _cfg(**overrides):
    cfg = {"governance_mode": "deterministic", "access_tier_groups": TIERS}
    cfg.update(overrides)
    return cfg


def _env(tmp_path, text, discovered=None, name="dev"):
    env = tmp_path / "envs" / name
    (env / "generated").mkdir(parents=True, exist_ok=True)
    (env / "data_access").mkdir(exist_ok=True)
    (env / "env.auto.tfvars").write_text(text)
    if discovered is not None:
        tables = sorted({t for ts in discovered.values() for t in ts})
        owners = {t: sorted(a for a, ts in discovered.items() if t in ts) for t in tables}
        (env / "data_access" / "discovered_uc_tables.auto.tfvars").write_text(
            "discovered_uc_tables = " + json.dumps(tables) + "\n"
            "discovered_table_agents = {\n" + "".join(f"  {json.dumps(t)} = {json.dumps(o)}\n" for t, o in owners.items()) + "}\n")
    return env


AGENT_ENV = '''governance_mode = "deterministic"
access_tier_groups = ["raw", "partial", "full"]
genie_spaces = [{ name = "Sales", genie_space_id = "space-1", acl_groups = ["partial", "raw"] }]
'''


# ── validation ────────────────────────────────────────────────────────────────

def test_table_readers_must_name_tier_groups_or_raw_exempt_principals():
    assert validate_config(_cfg(table_readers={"cat.sch.t": ["raw", "partial"], "cat.sch.*": ["etl"]},
                                raw_exempt_principals=["etl"])) == []
    errors = validate_config(_cfg(table_readers={"cat.sch.t": ["analysts"]}))
    assert errors == ['table_readers["cat.sch.t"] names \'analysts\', which is neither in access_tier_groups nor '
                      "in raw_exempt_principals; a reader outside the tiers would get an unplanned mask"]


@pytest.mark.parametrize("everyone", ["account users", "users", "Account Users"])
def test_account_users_is_never_a_reader_even_as_a_tier(everyone):
    errors = validate_config(_cfg(access_tier_groups=[everyone, "partial"], table_readers={"cat.sch.t": [everyone]}))
    assert any("never grants SELECT to everyone" in e for e in errors)


@pytest.mark.parametrize("key", ["cat.sch", "cat.*.t", "cat.sch.t*", "*.sch.t", "cat.sch.t.col", "cat..t"])
def test_table_readers_keys_use_the_uc_tables_syntax(key):
    assert any("must be catalog.schema.table" in e or "may use * only" in e
               for e in validate_config(_cfg(table_readers={key: ["raw"]})))


def test_schema_relative_keys_take_uc_catalog_and_may_not_collide():
    assert validate_config(_cfg(uc_catalog="cat", table_readers={"sch.t": ["raw"], "sch.*": ["full"]})) == []
    errors = validate_config(_cfg(uc_catalog="cat", table_readers={"sch.t": ["raw"], "cat.sch.t": ["full"]}))
    assert any("lists \"cat.sch.t\" twice" in e for e in errors)


# ── resolution ───────────────────────────────────────────────────────────────

def test_readers_are_the_union_of_every_matching_entry():
    readers = tr.normalize({"cat.sch.t": ["partial"], "cat.sch.*": ["raw"], "cat.other.*": ["full"], "sch.t": ["full"]}, "cat")
    assert tr.readers_for("cat.sch.t", readers) == ["full", "partial", "raw"]
    assert tr.readers_for("cat.sch.u", readers) == ["raw"]
    assert tr.readers_for("CAT.SCH.U", readers) == ["raw"]
    assert tr.readers_for("cat.other.t", readers) == ["full"]
    assert tr.readers_for("cat.third.t", readers) == []
    # An agent's catalog.schema.* pattern is covered only by that wildcard entry.
    assert tr.readers_for("cat.sch.*", readers) == ["raw"]


# ── refusals: acl_groups that can't read ─────────────────────────────────────

def test_access_check_names_the_table_and_the_group(tmp_path):
    env = _env(tmp_path, AGENT_ENV + 'table_readers = { "cat.sch.orders" = ["raw"] }\n',
               {"Sales": ["cat.sch.orders", "cat.sch.customers"]})
    problems = tr.access_problems(env)
    assert sorted(problems) == sorted([
        "agent Sales: acl_groups group 'partial' cannot read cat.sch.customers "
        '(add it to table_readers["cat.sch.customers"] or a matching catalog.schema.* entry)',
        "agent Sales: acl_groups group 'raw' cannot read cat.sch.customers "
        '(add it to table_readers["cat.sch.customers"] or a matching catalog.schema.* entry)',
        "agent Sales: acl_groups group 'partial' cannot read cat.sch.orders "
        '(add it to table_readers["cat.sch.orders"] or a matching catalog.schema.* entry)',
    ])
    assert tr.main(["check", "--env-dir", str(env), "--command-name", "make release"]) == 1


def test_a_wildcard_entry_satisfies_the_access_check(tmp_path):
    env = _env(tmp_path, AGENT_ENV + 'table_readers = { "cat.sch.*" = ["raw", "partial"] }\n',
               {"Sales": ["cat.sch.orders", "cat.sch.customers"]})
    assert tr.access_problems(env) == []


def test_a_deterministic_agent_without_acl_groups_stops_the_run(tmp_path):
    env = _env(tmp_path, 'governance_mode = "deterministic"\ngenie_spaces = [{ name = "Sales", uc_tables = ["cat.sch.t"] }]\n')
    assert tr.access_problems(env) == ["agent Sales has no acl_groups: list the groups that may run it ([] means nobody)"]
    env = _env(tmp_path, 'governance_mode = "deterministic"\ngenie_spaces = [{ name = "Sales", uc_tables = ["cat.sch.t"], acl_groups = [] }]\n')
    assert tr.access_problems(env) == []


def test_legacy_envs_are_not_refused_on_table_readers(tmp_path):
    legacy = _env(tmp_path, 'genie_spaces = [{ name = "Sales", uc_tables = ["cat.sch.t"], acl_groups = ["x"] }]\n')
    assert tr.access_problems(legacy) == []


# ── proposals written by make generate ───────────────────────────────────────

def test_generate_proposes_entries_only_for_uncovered_tables_of_new_agents(tmp_path):
    existing = 'table_readers = {\n  # reviewed\n  "cat.sch.orders" = ["raw"]\n}\n'
    env = _env(tmp_path, AGENT_ENV + 'raw_exempt_principals = ["etl"]\n' + existing,
               {"Sales": ["cat.sch.orders", "cat.sch.customers"]})
    assert tr.main(["propose", "--env-dir", str(env)]) == 0
    text = (env / "env.auto.tfvars").read_text()
    cfg = hcl2.loads(text)
    # The existing entry is untouched, byte for byte; the uncovered table gets acl_groups.
    assert '  # reviewed\n  "cat.sch.orders" = ["raw"]\n' in text
    assert cfg["table_readers"] == {"cat.sch.orders": ["raw"], "cat.sch.customers": ["partial", "raw"]}
    assert json.loads((env / tr.PROPOSED_RECORD).read_text()) == {"agents": ["Sales"]}
    # The existing entry still doesn't list partial: the check names it (never widened silently).
    assert tr.access_problems(env) == [
        "agent Sales: acl_groups group 'partial' cannot read cat.sch.orders "
        '(add it to table_readers["cat.sch.orders"] or a matching catalog.schema.* entry)']


def test_proposals_happen_once_per_agent_so_a_removed_entry_stays_removed(tmp_path):
    env = _env(tmp_path, AGENT_ENV, {"Sales": ["cat.sch.orders"]})
    tr.main(["propose", "--env-dir", str(env)])
    assert hcl2.loads((env / "env.auto.tfvars").read_text())["table_readers"] == {"cat.sch.orders": ["partial", "raw"]}
    # The reviewer removes it (to revoke): the next generate doesn't put it back.
    (env / "env.auto.tfvars").write_text(AGENT_ENV + "table_readers = {}\n")
    tr.main(["propose", "--env-dir", str(env)])
    assert hcl2.loads((env / "env.auto.tfvars").read_text())["table_readers"] == {}


def test_proposals_leave_out_groups_that_cannot_be_readers(tmp_path):
    env = _env(tmp_path, AGENT_ENV.replace('["partial", "raw"]', '["partial", "analysts", "account users"]'),
               {"Sales": ["cat.sch.orders"]})
    tr.main(["propose", "--env-dir", str(env)])
    cfg = hcl2.loads((env / "env.auto.tfvars").read_text())
    assert cfg["table_readers"] == {"cat.sch.orders": ["partial"]}
    assert validate_config(cfg) == []
    assert any("'analysts' cannot read cat.sch.orders" in p for p in tr.access_problems(env))


def test_no_proposals_for_legacy(tmp_path):
    env = _env(tmp_path, AGENT_ENV.replace('governance_mode = "deterministic"\n', ""), {"Sales": ["cat.sch.orders"]})
    assert tr.proposals(env) == ({}, [])


def test_show_lists_resolved_readers_and_revocations(tmp_path):
    env = _env(tmp_path, AGENT_ENV + 'uc_tables = ["cat.sch.orders"]\ntable_readers = { "cat.sch.*" = ["raw"] }\n')
    (env / "data_access" / "terraform.tfstate").write_text(json.dumps({"resources": [{
        "module": "module.data_access", "mode": "managed", "type": "databricks_grant", "name": "table_access",
        "instances": [{"index_key": "cat.sch.orders|raw"}, {"index_key": "cat.sch.orders|partial"}]}]}))
    lines = tr.describe(env)
    assert "  cat.sch.orders: raw" in lines
    assert lines[-2:] == ["Revokes SELECT (applied, but no table_readers entry grants it any more):",
                          "  cat.sch.orders|partial"]


# ── upgrade: an existing deterministic env with no table_readers ─────────────

def test_upgrade_existing_env_is_refused_until_table_readers_cover_its_agents(tmp_path):
    """Step-7 envs have agents with acl_groups and no table_readers. release
    refuses (naming table and group) instead of granting CAN_RUN to groups
    that can't read; generate proposes entries that cover every agent table,
    after which the check passes. Terraform separately refuses to plan when
    table_readers is unset but the state holds SELECT grants
    (table_readers.tftest.hcl: unset_table_readers_refuses_to_revoke_applied_grants),
    and keeps every applied grant the proposal covers
    (proposed_readers_keep_every_applied_grant)."""
    env = _env(tmp_path, AGENT_ENV, {"Sales": ["cat.sch.orders", "cat.sch.customers"]})
    assert len(tr.access_problems(env)) == 4
    assert tr.describe(env) == ["Table readers: table_readers is not set, so GenieRails grants no SELECT "
                                "(masks apply to whoever already has access)."]
    assert tr.main(["propose", "--env-dir", str(env)]) == 0
    assert tr.access_problems(env) == []
    assert hcl2.loads((env / "env.auto.tfvars").read_text())["table_readers"] == {
        "cat.sch.customers": ["partial", "raw"], "cat.sch.orders": ["partial", "raw"]}


# ── live agent coverage check ────────────────────────────────────────────────

VALUE = "email_partial__string__0a1b2c3d"
# A GenieRails policy, by its deterministic name and function.
OWN = {"name": f"cat_gr_mask_cat_{VALUE}_partial", "function_name": f"cat.sch.gr_mask_{VALUE}_partial",
       "policy_type": "POLICY_TYPE_COLUMN_MASK", "conditions": [f"hasTagValue('gr_treatment', '{VALUE}')"]}


def _snapshot(**overrides):
    base = dict(
        column_tags={"cat.sch.t": {"email": {("class.email_address", ""), ("gr_treatment", VALUE)},
                                   "id": {("other", "x")}}},
        policies={"cat.sch.t": [OWN]},
        grants={("CATALOG", "cat"): {("partial", "USE_CATALOG")},
                ("SCHEMA", "cat.sch"): {("partial", "USE SCHEMA")},
                ("TABLE", "cat.sch.t"): {("partial", "SELECT")}},
    )
    base.update(overrides)
    return ac.Snapshot(**base)


AGENT = {"a": {"name": "A", "groups": ["partial"], "tables": ["cat.sch.t"]}}


def test_live_check_passes_when_every_classified_column_is_masked_and_groups_can_read():
    assert ac.evaluate(AGENT, _snapshot())["a"]["status"] == "pass"


def test_live_check_fails_an_unmasked_classified_column():
    result = ac.evaluate(AGENT, _snapshot(policies={}))["a"]
    assert result["status"] == "fail"
    assert result["problems"] == ["cat.sch.t.email is classified (class.email_address) but no GenieRails mask applies to it"]


@pytest.mark.parametrize("snapshot,note", [
    # A table-attached mask (made in the UI or by another tool) doesn't count.
    ({"policies": {}, "table_masks": {"cat.sch.t.email"}}, " (only a table-attached mask, which GenieRails doesn't support)"),
    # Nor does a policy GenieRails didn't create, whatever it matches.
    ({"policies": {"cat.sch.t": [{**OWN, "name": "customer_email_mask"}]}}, ""),
    ({"policies": {"cat.sch.t": [{**OWN, "function_name": "cat.sch.my_mask"}]}}, ""),
    # A GenieRails policy that doesn't match one of the column's tags doesn't either.
    ({"policies": {"cat.sch.t": [{**OWN, "conditions": ["hasTagValue('gr_treatment', 'other')"]}]}}, ""),
    # And a row filter is never a mask.
    ({"policies": {"cat.sch.t": [{**OWN, "policy_type": "POLICY_TYPE_ROW_FILTER"}]}}, ""),
])
def test_live_check_accepts_only_genierails_masks(snapshot, note):
    result = ac.evaluate(AGENT, _snapshot(**snapshot))["a"]
    assert result["status"] == "fail"
    assert result["problems"] == [
        f"cat.sch.t.email is classified (class.email_address) but no GenieRails mask applies to it{note}"]


def test_an_inherited_genierails_policy_matching_by_key_counts():
    snapshot = _snapshot(policies={"cat.sch.t": [{**OWN, "conditions": ["hasTag('gr_treatment')"]}]})
    assert ac.evaluate(AGENT, snapshot)["a"]["status"] == "pass"


def test_live_check_fails_a_group_that_cannot_read():
    snapshot = _snapshot(grants={("CATALOG", "cat"): {("partial", "USE_CATALOG")}, ("TABLE", "cat.sch.t"): {("partial", "SELECT")}})
    assert ac.evaluate(AGENT, snapshot)["a"]["problems"] == ["group 'partial' cannot read cat.sch.t (no USE SCHEMA)"]


@pytest.mark.parametrize("grants,groups,owners", [
    # Nested in a group that holds the grants.
    ({("CATALOG", "cat"): {("parent", "USE CATALOG")}, ("SCHEMA", "cat.sch"): {("parent", "USE_SCHEMA")},
      ("TABLE", "cat.sch.t"): {("parent", "SELECT")}}, {"parent": {"mid"}, "mid": {"partial"}}, {}),
    # Inherited: catalog-level privileges.
    ({("CATALOG", "cat"): {("partial", "USE_CATALOG"), ("partial", "USE_SCHEMA"), ("partial", "SELECT")}}, {}, {}),
    # account users holds them.
    ({("CATALOG", "cat"): {("account users", "ALL_PRIVILEGES")}}, {}, {}),
    # It owns the table.
    ({}, {}, {("TABLE", "cat.sch.t"): "partial"}),
])
def test_live_check_reads_inherited_and_nested_access(grants, groups, owners):
    snapshot = _snapshot(grants=grants, groups=groups, owners=owners)
    assert ac.evaluate(AGENT, snapshot)["a"]["status"] == "pass"


def test_live_check_fails_a_missing_table_and_an_agent_with_no_tables():
    assert ac.evaluate(AGENT, _snapshot(missing={"cat.sch.t"}))["a"]["problems"] == [
        "cat.sch.t does not exist or is not visible to the deployer"]
    empty = {"a": {"name": "A", "groups": ["partial"], "tables": []}}
    assert ac.evaluate(empty, ac.Snapshot())["a"]["status"] == "fail"


def _console_runner(tmp_path, value, exit_code=0):
    runner = tmp_path / "runner.sh"
    encoded = base64.b64encode(json.dumps(value).encode()).decode()
    runner.write_text(f"#!/bin/sh\ncat >/dev/null\necho '+ terraform console'\necho '\"{encoded}\"'\nexit {exit_code}\n")
    runner.chmod(0o755)
    return runner


class _FakeLive:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def expand(self, tables):
        return sorted(tables)

    def read(self, tables):
        return self.snapshot


INPUTS = {"fingerprint": "fp-1", "inputs": {"agents": {
    "a": {"name": "A", "groups": ["partial"], "tables": ["cat.sch.t"]},
    "b": {"name": "B", "groups": [], "tables": []},
}}}


def test_run_records_a_pass_per_agent_for_the_terraform_fingerprint(tmp_path):
    env = _env(tmp_path, AGENT_ENV)
    rc = ac.run(env, "dev", _console_runner(tmp_path, INPUTS), "", snapshot_factory=lambda _d: _FakeLive(_snapshot()))
    record = json.loads((env / ac.RESULT_FILENAME).read_text())
    assert rc == 0
    assert record["status"] == "pass" and record["fingerprint"] == "fp-1" and record["refreshed_at"]
    assert {k: v["status"] for k, v in record["agents"].items()} == {"a": "pass", "b": "pass"}


def test_run_records_a_failed_agent_and_strict_exits_non_zero(tmp_path):
    env = _env(tmp_path, AGENT_ENV)
    factory = lambda _d: _FakeLive(_snapshot(policies={}))  # noqa: E731
    assert ac.run(env, "dev", _console_runner(tmp_path, INPUTS), "", snapshot_factory=factory) == 0
    assert ac.run(env, "dev", _console_runner(tmp_path, INPUTS), "", strict=True, snapshot_factory=factory) == 1
    record = json.loads((env / ac.RESULT_FILENAME).read_text())
    assert record["agents"]["a"]["status"] == "fail"


@pytest.mark.parametrize("failure", ["console", "live"])
def test_run_fails_closed_when_it_cannot_check(tmp_path, failure):
    env = _env(tmp_path, AGENT_ENV)
    (env / ac.RESULT_FILENAME).write_text(json.dumps({"status": "pass", "fingerprint": "fp-1", "refreshed_at": "x",
                                                      "agents": {"a": {"status": "pass"}}}))

    def broken(_d):
        raise RuntimeError("warehouse unavailable")

    runner = _console_runner(tmp_path, INPUTS, exit_code=1 if failure == "console" else 0)
    assert ac.run(env, "dev", runner, "", snapshot_factory=broken) == 2
    record = json.loads((env / ac.RESULT_FILENAME).read_text())
    # No older pass survives: a failed result with no refresh time and no agents.
    assert record["status"] == "fail" and "refreshed_at" not in record and record["agents"] == {}


def test_run_is_a_no_op_for_legacy_envs(tmp_path):
    env = _env(tmp_path, 'genie_spaces = []\n')
    assert ac.run(env, "dev", tmp_path / "missing-runner", "") == 0
    assert not (env / ac.RESULT_FILENAME).exists()


def test_run_adds_the_tables_generate_attributed_to_agents_known_by_id(tmp_path):
    env = _env(tmp_path, AGENT_ENV, {"A": ["cat.sch.t"]})
    value = {"fingerprint": "fp", "inputs": {"agents": {"a": {"name": "A", "groups": ["partial"], "tables": []}}}}
    seen = []

    class Live(_FakeLive):
        def read(self, tables):
            seen.extend(tables)
            return super().read(tables)

    ac.run(env, "dev", _console_runner(tmp_path, value), "", snapshot_factory=lambda _d: Live(_snapshot()))
    assert seen == ["cat.sch.t"]


# ── release summary follows the mode ─────────────────────────────────────────

def test_release_summary_never_claims_access_nothing_grants(tmp_path):
    gov_only = _env(tmp_path, 'governance_mode = "deterministic"\nuc_tables = ["cat.sch.t"]\n', name="a")
    assert release_helpers.access_summary(gov_only) == [
        "  Table access:  none granted (no table_readers); masks apply to whoever already has access",
        "  Agent access:  none (no agent with acl_groups)"]
    readers = _env(tmp_path, AGENT_ENV + 'table_readers = { "cat.sch.*" = ["raw"] }\n', name="b")
    assert release_helpers.access_summary(readers)[0].startswith("  Table access:  SELECT from table_readers")
    assert "CAN_RUN from acl_groups" in release_helpers.access_summary(readers)[1]


# ── promotion: env-owned table_readers and governance ────────────────────────

REMAP = SHARED / "scripts" / "remap_env_config.py"


def _promote(tmp_path, source_text, dest_text):
    source = tmp_path / "envs" / "dev"
    dest = tmp_path / "envs" / "prod"
    source.mkdir(parents=True, exist_ok=True)
    (source / "env.auto.tfvars").write_text(source_text)
    if dest_text is not None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "env.auto.tfvars").write_text(dest_text)
    result = subprocess.run([sys.executable, str(REMAP), str(source), str(dest), "dev_cat=prod_cat"],
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return hcl2.loads((dest / "env.auto.tfvars").read_text()), result.stdout


SOURCE = '''governance_mode = "deterministic"
uc_catalog = "dev_cat"
access_tier_groups = ["dev_raw", "dev_partial", "full"]
genie_spaces = [{ name = "Sales", uc_tables = ["dev_cat.sch.t"], acl_groups = ["dev_partial", "dev_etl"] }]
uc_tables = ["dev_cat.sch.t"]
table_readers = { "sch.t" = ["dev_raw", "dev_etl"], "dev_cat.sch.*" = ["dev_partial"] }
raw_exempt_principals = ["dev_etl"]
row_filters = [{ table = "dev_cat.sch.t", column = "region", values_by_group = { dev_partial = ["APAC"] } }]
'''
PROD = 'governance_mode = "deterministic"\n'
GROUP_MAP = 'group_map = { dev_raw = "prod_raw", dev_partial = "prod_partial", dev_etl = "prod_etl" }\n'


def test_first_promote_seeds_table_readers_with_remapped_catalogs(tmp_path):
    cfg, out = _promote(tmp_path, SOURCE, PROD)
    assert cfg["table_readers"] == {"prod_cat.sch.*": ["dev_partial"], "prod_cat.sch.t": ["dev_raw", "dev_etl"]}
    assert "raw_exempt_principals" not in cfg  # env-owned, never copied from dev
    assert "Seeded table_readers from dev" in out


def test_later_promotes_keep_the_destination_table_readers_and_print_differences(tmp_path):
    dest = PROD + 'raw_exempt_principals = ["prod-etl"]\ntable_readers = { "prod_cat.sch.t" = ["full"] }\n'
    cfg, out = _promote(tmp_path, SOURCE, dest)
    assert cfg["table_readers"] == {"prod_cat.sch.t": ["full"]}
    assert cfg["raw_exempt_principals"] == ["prod-etl"]
    assert ("table_readers diff for prod_cat.sch.t (dev vs destination): dev=['dev_etl', 'dev_raw']; "
            "destination=['full']") in out


# ── group_map (decision 8) ───────────────────────────────────────────────────

def test_promote_maps_every_group_it_writes_and_never_raw_exempt_principals(tmp_path):
    cfg, out = _promote(tmp_path, SOURCE, PROD + GROUP_MAP.replace(', dev_etl = "prod_etl"', ""))
    # Tiers are promoted governance, through group_map (unmapped groups keep their names).
    assert cfg["access_tier_groups"] == ["prod_raw", "prod_partial", "full"]
    # First seeding of table_readers and acl_groups is mapped; dev's
    # raw_exempt principal (dev_etl) is never mapped.
    assert cfg["table_readers"] == {"prod_cat.sch.*": ["prod_partial"], "prod_cat.sch.t": ["prod_raw", "dev_etl"]}
    assert cfg["genie_spaces"][0]["acl_groups"] == ["prod_partial", "dev_etl"]
    # Row-filter groups, with the table remapped.
    assert cfg["row_filters"] == [{"table": "prod_cat.sch.t", "column": "region",
                                   "values_by_group": {"prod_partial": ["APAC"]}}]
    # group_map itself is the destination's and stays.
    assert cfg["group_map"] == {"dev_raw": "prod_raw", "dev_partial": "prod_partial"}
    assert "group_map: dev_partial -> prod_partial, dev_raw -> prod_raw" in out
    # prod lists its own raw_exempt_principals: until it does, the unmapped dev one is refused as a reader.
    assert [e for e in validate_config(cfg)] == [
        'table_readers["prod_cat.sch.t"] names \'dev_etl\', which is neither in access_tier_groups nor in '
        "raw_exempt_principals; a reader outside the tiers would get an unplanned mask"]


def test_tiers_are_promoted_on_every_promote_but_seeded_access_is_kept(tmp_path):
    _promote(tmp_path, SOURCE, PROD + GROUP_MAP)
    dest = tmp_path / "envs" / "prod" / "env.auto.tfvars"
    text = dest.read_text().replace('"prod_partial", "dev_etl"]', '"prod_partial"]')  # a prod-only ACL edit
    assert text != dest.read_text()
    dest.write_text(text)
    source = (tmp_path / "envs" / "dev" / "env.auto.tfvars")
    source.write_text(SOURCE.replace('"dev_partial", "full"]', '"dev_partial", "viewers"]'))
    cfg, out = _promote(tmp_path, source.read_text(), None)
    assert cfg["access_tier_groups"] == ["prod_raw", "prod_partial", "viewers"]
    assert "access_tier_groups promoted" in out
    assert cfg["genie_spaces"][0]["acl_groups"] == ["prod_partial"]


def test_legacy_destinations_keep_their_own_tiers(tmp_path):
    cfg, _ = _promote(tmp_path, SOURCE.replace('governance_mode = "deterministic"\n', ""),
                      'access_tier_groups = ["stale"]\n' + GROUP_MAP.replace("group_map", "# group_map"))
    assert cfg["access_tier_groups"] == ["stale"]
    assert "group_map" not in cfg


def test_promoted_policy_principals_go_through_group_map(tmp_path):
    sys.path.insert(0, str(SHARED / "scripts"))
    from remap_generated_config import map_principals

    hcl = ('  {\n    to_principals    = ["dev_partial"]\n    except_principals = ["dev_raw", "dev_etl", "sp-1"]\n  }\n')
    mapped = map_principals(hcl, {"dev_partial": "prod_partial", "dev_raw": "prod_raw", "dev_etl": "x"}, {"dev_etl"})
    assert '    to_principals    = ["prod_partial"]\n' in mapped
    assert '    except_principals = ["prod_raw", "dev_etl", "sp-1"]\n' in mapped


@pytest.mark.parametrize("group_map,error", [
    ({"a": "p", "b": "p"}, "maps a, b all to 'p': many-to-one mappings"),
    ({"etl": "prod_etl"}, "maps 'etl', which is in raw_exempt_principals"),
    ({"account users": "x"}, "account users is never mapped"),
    ({"a": ""}, "must map dev group names"),
])
def test_group_map_validation(group_map, error):
    errors = validate_config(_cfg(group_map=group_map, raw_exempt_principals=["etl"]))
    assert any(error in e for e in errors), errors


def test_group_map_is_deterministic_only():
    assert validate_config({"group_map": {"a": "b"}}) == ['group_map applies only with governance_mode = "deterministic"']


def test_a_many_to_one_destination_map_stops_promote(tmp_path):
    source = tmp_path / "envs" / "dev"
    dest = tmp_path / "envs" / "prod"
    source.mkdir(parents=True)
    dest.mkdir(parents=True)
    (source / "env.auto.tfvars").write_text(SOURCE)
    (dest / "env.auto.tfvars").write_text(PROD + 'group_map = { dev_raw = "p", dev_partial = "p" }\n')
    result = subprocess.run([sys.executable, str(REMAP), str(source), str(dest), "dev_cat=prod_cat"],
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 1 and "many-to-one" in result.stdout and "nothing was written" in result.stdout


import group_map as gm  # noqa: E402

MAPPED_PROD = '''governance_mode = "deterministic"
access_tier_groups = ["prod_raw", "prod_partial", "full"]
group_map = { dev_raw = "prod_raw", dev_partial = "prod_partial" }
genie_spaces = [{ name = "Sales", uc_tables = ["c.s.t"], acl_groups = ["prod_partial"] }]
table_readers = { "c.s.t" = ["prod_partial"] }
row_filters = [{ table = "c.s.t", column = "r", values_by_group = { prod_rf = ["x"] } }]
'''


def _check(env, existing):
    return gm.main(["check", "--env-dir", str(env), "--env", "prod", "--command-name", "make release"],
                   lister=lambda _d: existing)


def test_every_resulting_group_must_exist_live_mapped_or_not(tmp_path, capsys):
    env = _env(tmp_path, MAPPED_PROD, name="prod")
    assert _check(env, ["prod_raw", "prod_partial", "full", "prod_rf"]) == 0
    assert _check(env, ["prod_raw", "prod_partial", "prod_rf"]) == 1  # unmapped "full" is missing
    assert "don't exist in prod (after group_map): full" in capsys.readouterr().err


def test_the_group_check_fails_closed_when_groups_cannot_be_listed(tmp_path, capsys):
    env = _env(tmp_path, MAPPED_PROD, name="prod")

    def broken(_d):
        raise RuntimeError("SCIM unavailable")

    assert gm.main(["check", "--env-dir", str(env), "--env", "prod"], lister=broken) == 2
    assert "could not run: SCIM unavailable" in capsys.readouterr().err


def test_a_changed_group_map_needs_its_ack(tmp_path, capsys):
    existing = ["prod_raw", "prod_partial", "full", "prod_rf", "prod_viewers"]
    env = _env(tmp_path, MAPPED_PROD, name="prod")
    (env / "data_access" / "terraform.tfstate").write_text("{}")  # applied before
    # First release: the applied map is recorded.
    gm.record_applied(env, gm._load_hcl(env / "env.auto.tfvars"))
    assert _check(env, existing) == 0
    # Retarget one mapping, add one, remove one.
    (env / "env.auto.tfvars").write_text(MAPPED_PROD.replace(
        'group_map = { dev_raw = "prod_raw", dev_partial = "prod_partial" }',
        'group_map = { dev_raw = "prod_viewers", dev_viewers = "prod_viewers2" }'))
    existing.append("prod_viewers2")
    capsys.readouterr()
    assert _check(env, existing) == 1
    err = capsys.readouterr().err
    for key in ("prod:weaken:group_map:dev_raw=prod_viewers", "prod:weaken:group_map:dev_viewers=prod_viewers2",
                "prod:weaken:group_map:dev_partial=dev_partial"):
        assert key in err
    (env / "ack.txt").write_text("prod:weaken:group_map:dev_raw=prod_viewers\n"
                                 "prod:weaken:group_map:dev_viewers=prod_viewers2\n")
    assert _check(env, existing) == 1
    assert "dev_partial=dev_partial" in capsys.readouterr().err
    (env / "ack.txt").write_text((env / "ack.txt").read_text() + "prod:weaken:group_map:dev_partial=dev_partial\n")
    assert _check(env, existing) == 0
    # An ack for another env is refused by the step-7 parser.
    (env / "ack.txt").write_text("dev:weaken:group_map:dev_raw=prod_viewers\n")
    assert _check(env, existing) == 2


def test_an_already_applied_env_without_a_record_counts_a_new_group_map_as_a_change(tmp_path, capsys):
    existing = ["prod_raw", "prod_partial", "full", "prod_rf"]
    fresh = _env(tmp_path, MAPPED_PROD, name="prod")
    assert _check(fresh, existing) == 0  # first release of a fresh env
    applied = _env(tmp_path, MAPPED_PROD, name="other")
    (applied / "terraform.tfstate").write_text("{}")
    assert gm.main(["check", "--env-dir", str(applied), "--env", "other"], lister=lambda _d: existing) == 1
    assert "other:weaken:group_map:dev_raw=prod_raw" in capsys.readouterr().err


def test_nested_groups_are_expanded_after_mapping(tmp_path):
    # The live check reads the destination's own, mapped config: prod_partial
    # reads through its prod parent; dev_partial's membership is irrelevant.
    snapshot = _snapshot(grants={("CATALOG", "cat"): {("prod_parent", "USE_CATALOG"), ("prod_parent", "USE_SCHEMA"),
                                                      ("prod_parent", "SELECT")}},
                         groups={"prod_parent": {"prod_partial"}, "dev_parent": {"dev_partial"}})
    mapped = {"a": {"name": "A", "groups": gm.map_groups(["dev_partial"], {"dev_partial": "prod_partial"}),
                    "tables": ["cat.sch.t"]}}
    assert ac.evaluate(mapped, snapshot)["a"]["status"] == "pass"


def test_legacy_envs_skip_the_group_check(tmp_path):
    env = _env(tmp_path, 'uc_tables = ["c.s.t"]\n', name="prod")
    assert gm.main(["check", "--env-dir", str(env), "--env", "prod"], lister=lambda _d: []) == 0


# ── Makefile: refusals, group check, governance-only messages ─────────────

def _clean_env():
    return {k: v for k, v in os.environ.items() if k not in ("MAKEFLAGS", "MAKELEVEL", "ENV", "CI")}


def _make(tmp_path, env, *args):
    log = tmp_path / "make-calls"
    stub = tmp_path / "make-stub"
    stub.write_text(f"#!/bin/sh\nprintf 'make %s\\n' \"$*\" >> '{log}'\nexit 0\n")
    stub.chmod(0o755)
    result = subprocess.run(["make", "--no-print-directory", *args, f"ENV_DIR={env}", f"MAKE={stub}"],
                            cwd=ROOT / "aws", text=True, capture_output=True, env=_clean_env(), timeout=120)
    return result, (log.read_text().splitlines() if log.exists() else [])


@needs_make
@pytest.mark.parametrize("target,env_name", [("rehearse", "dev"), ("release", "prod")])
def test_rehearse_and_release_refuse_acl_groups_that_cannot_read_before_anything_runs(tmp_path, target, env_name):
    env = _env(tmp_path, AGENT_ENV + 'uc_tables = ["cat.sch.t"]\n', {"Sales": ["cat.sch.t"]}, name=env_name)
    result, calls = _make(tmp_path, env, target, f"ENV={env_name}")
    assert result.returncode != 0
    assert f"make {target} refused" in result.stderr
    assert "acl_groups group 'partial' cannot read cat.sch.t" in result.stderr
    assert calls == []
    assert not (env / "generated" / ".governance.lock").exists()


@needs_make
@pytest.mark.parametrize("target,env_name", [("rehearse", "dev"), ("release", "prod")])
def test_rehearse_and_release_stop_on_the_group_check_before_anything_runs(tmp_path, target, env_name):
    env = _env(tmp_path, AGENT_ENV + 'table_readers = { "cat.sch.*" = ["raw", "partial"] }\n',
               {"Sales": ["cat.sch.t"]}, name=env_name)
    check = tmp_path / "group-check"
    check.write_text(f"#!/bin/sh\necho \"$*\" > '{tmp_path / 'group-args'}'\nexit 1\n")
    check.chmod(0o755)
    result, calls = _make(tmp_path, env, target, f"ENV={env_name}", f"_GROUP_CHECK={check}")
    assert result.returncode != 0
    assert (tmp_path / "group-args").read_text().strip() == f"make {target}"
    assert calls == []
    assert not (env / "generated" / ".governance.lock").exists()


@needs_make
def test_deterministic_workspace_plan_runs_the_live_check_before_the_can_run_check(tmp_path):
    env = _env(tmp_path, AGENT_ENV)
    (env / "abac.auto.tfvars").write_text("# workspace config\n")
    log = tmp_path / "calls"
    stubs = {}
    for name in ("agent", "gate", "runner"):
        path = tmp_path / name
        path.write_text(f"#!/bin/sh\nprintf '{name} %s\\n' \"$*\" >> '{log}'\nexit 0\n")
        path.chmod(0o755)
        stubs[name] = path
    result = subprocess.run(
        ["make", "--no-print-directory", "_plan-layer", "LAYER=workspace", "TARGET_ENV=dev", f"LAYER_ENV_DIR={env}",
         f"_AGENT_COVERAGE={stubs['agent']}", f"_COVERAGE_GATE={stubs['gate']}", f"ROOT_RUNNER={stubs['runner']}",
         f"GENIE_ADOPT_PREFLIGHT_SCRIPT={SHARED / 'scripts' / 'genie_adopt_preflight.py'}"],
        cwd=ROOT / "aws", env=_clean_env(), text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text().splitlines()
    first = [c.split()[0] + " " + c.split()[1] for c in calls]
    assert first.index("agent run") < first.index("gate can-run-check") < first.index("runner workspace")


@needs_make
def test_maintain_skips_the_genie_id_preflight_without_agents(tmp_path):
    env = _env(tmp_path, 'governance_mode = "deterministic"\nuc_tables = ["cat.sch.t"]\n', name="prod")
    preflight = tmp_path / "preflight.py"
    preflight.write_text(f"open({str(tmp_path / 'preflight-called')!r}, 'w').write('x')\n")
    audit = tmp_path / "audit.py"
    audit.write_text("raise SystemExit(0)\n")
    result, _calls = _make(tmp_path, env, "maintain", "ENV=prod", f"GENIE_ADOPT_PREFLIGHT_SCRIPT={preflight}",
                           f"AUDIT_SCHEMA_SCRIPT={audit}", f"SHARED_ROOT={SHARED}")
    # The external-mask inventory needs Databricks; whatever it does, the
    # preflight must never be the thing that ran for a governance-only env.
    assert not (tmp_path / "preflight-called").exists(), result.stdout + result.stderr


@needs_make
def test_setup_prints_governance_only_steps(tmp_path):
    cloud = tmp_path / "aws"
    env = cloud / "envs" / "dev"
    env.mkdir(parents=True)
    (env / "env.auto.tfvars").write_text('governance_mode = "deterministic"\nuc_tables = ["cat.sch.t"]\n')
    result = subprocess.run(["make", "--no-print-directory", "setup", "ENV=dev", f"CLOUD_ROOT={cloud}", f"SHARED_ROOT={SHARED}"],
                            cwd=ROOT / "aws", env=_clean_env(), text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "governance only (no agent): set uc_tables" in result.stdout
    assert "set genie_space_id" not in result.stdout
    assert "Genie CAN_RUN" not in result.stdout
    assert "(governance only: applies masks; grants SELECT only from table_readers" in result.stdout


# ── proof: the Terraform tests fail when a protection is reverted ───────────

MUTATIONS = {
    # Agents' acl_groups grant SELECT again in deterministic envs.
    "acl_groups_grant_select": (
        "modules/data_access/main.tf",
        'table_access_pairs = var.governance_mode == "deterministic" ? local.reader_access_pairs : flatten([',
        'table_access_pairs = var.governance_mode == "deterministic" ? concat(local.reader_access_pairs, flatten([for t, ps in local.scoped_table_access_principals : [for p in ps : { table = t, principal = p }]])) : flatten([',
        "modules/data_access", "tests/table_readers.tftest.hcl",
    ),
    # A reader outside the tiers (or account users) is no longer refused.
    "readers_outside_tiers_allowed": (
        "modules/data_access/outputs.tf",
        "condition     = length(local.table_readers_refused) == 0",
        "condition     = length(local.table_readers_refused) >= 0",
        "modules/data_access", "tests/table_readers.tftest.hcl",
    ),
    # An unset table_readers silently revokes applied grants.
    "unset_table_readers_revokes": (
        "modules/data_access/outputs.tf",
        "condition     = !(local.grants_from_readers && var.table_readers == null && length(var.applied_table_grants) > 0)",
        "condition     = length(var.applied_table_grants) >= 0",
        "modules/data_access", "tests/table_readers.tftest.hcl",
    ),
    # CAN_RUN no longer waits for the live agent coverage check.
    "can_run_ignores_live_check": (
        "roots/workspace/main.tf",
        "genie_exposure_static_blocker = local.deterministic ? local.agent_gate_static_blocker : (",
        'genie_exposure_static_blocker = local.deterministic ? "" : (',
        "roots/workspace", "tests/agent_coverage_gate.tftest.hcl",
    ),
    # A failed agent opens anyway.
    "failed_agent_opens": (
        "roots/workspace/main.tf",
        "genie_space_missing_grants = local.deterministic ? local.agent_gate_missing : {",
        "genie_space_missing_grants = local.deterministic ? { for key, space in local.merged_spaces : key => [] } : {",
        "roots/workspace", "tests/agent_coverage_gate.tftest.hcl",
    ),
    # CAN_RUN comes from drafted/derived ACLs instead of acl_groups.
    "can_run_from_drafted_acls": (
        "roots/workspace/main.tf",
        "      config = (local.deterministic\n",
        "      config = (false\n",
        "roots/workspace", "tests/agent_coverage_gate.tftest.hcl",
    ),
}


@pytest.fixture(scope="module")
def tf_plugin_cache(tmp_path_factory):
    from tests.terraform_helpers import session_plugin_cache
    return session_plugin_cache()


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform not installed")
@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
def test_reverting_a_protection_fails_its_terraform_test(mutation, tmp_path, tf_plugin_cache):
    from tests.terraform_helpers import shared_copy, tf, tf_env, tf_init

    path, old, new, config, test_file = MUTATIONS[mutation]
    copy = shared_copy(tmp_path)
    target = copy / path
    source = target.read_text()
    assert source.count(old) == 1, f"{mutation}: the protected text moved; update this proof"
    target.write_text(source.replace(old, new))
    directory = copy / config
    env = tf_env(tmp_path, tf_plugin_cache)
    tf_init(directory, env=env)
    result = tf(directory, "test", "-no-color", f"-filter={test_file}", env=env)
    assert result.returncode != 0 and "failed" in result.stdout, (
        f"{mutation}: {test_file} still passes with the protection reverted\n" + result.stdout[-3000:])


# ── legacy envs plan zero changes against main ──────────────────────────────

BASE_COMMIT = "864f59f"  # origin/main this step is built on

LEGACY_DATA_ACCESS_TEST = '''
mock_provider "databricks" {
  alias = "account"
}
mock_provider "databricks" {
  alias = "workspace"
}
mock_provider "null" {}
mock_provider "time" {}

variables {
  env_dir                   = "tests/.tmp/legacy0/data_access"
  databricks_account_id     = "account"
  databricks_client_id      = "service-principal"
  databricks_client_secret  = "secret"
  databricks_workspace_host = "https://example.invalid"
  databricks_workspace_id   = "123"
  sql_warehouse_id          = "warehouse"
  uc_tables                 = ["cat.sch.customers"]
  groups                    = { analysts = {}, auditors = {} }
  genie_spaces              = [{ name = "Sales", genie_space_id = "space-1", uc_tables = ["cat.sch.customers"], acl_groups = ["analysts"] }]
  genie_space_acl_groups    = { Sales = ["analysts"] }
  tag_assignments = [{ entity_type = "columns", entity_name = "cat.sch.customers.email", tag_key = "pii_level", tag_value = "masked" }]
  fgac_policies = [{
    name = "mask_email", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat",
    to_principals = ["auditors"], except_principals = ["analysts"],
    match_condition = "hasTagValue('pii_level', 'masked')", match_alias = "email",
    function_name = "mask_email", function_catalog = "cat", function_schema = "sch",
  }]
}

run "setup" {
  module {
    source = "./tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/legacy0/data_access/masking_functions.sql" = "SELECT 1;\\n"
      "tests/.tmp/legacy0/ddl/_fetched.sql"                  = "CREATE TABLE cat.sch.customers (\\n  id BIGINT,\\n  email STRING\\n);\\n"
    }
  }
}

run "main_gate_inputs" {
  state_key = "legacy"
  module {
    source = "./legacy/shared/roots/data_access"
  }
  command = plan
  plan_options {
    mode = refresh-only
  }
  expect_failures = [check.business_select_withheld]
}

run "gate_passes" {
  module {
    source = "./tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/legacy0/data_access/masking_functions.sql" = "SELECT 1;\\n"
      "tests/.tmp/legacy0/ddl/_fetched.sql"                  = "CREATE TABLE cat.sch.customers (\\n  id BIGINT,\\n  email STRING\\n);\\n"
      "tests/.tmp/legacy0/data_access/.coverage_gate.json"   = jsonencode({ status = "pass", fingerprint = run.main_gate_inputs.coverage_gate_inputs.fingerprint, refreshed_at = "@NOW@" })
    }
  }
}

run "applied_by_main" {
  state_key = "legacy"
  module {
    source = "./legacy/shared/roots/data_access"
  }
}

run "planned_by_this_branch" {
  state_key = "legacy"
  command   = plan
}
'''

LEGACY_WORKSPACE_TEST = '''
mock_provider "databricks" {
  alias = "account"
}
mock_provider "databricks" {
  alias = "workspace"
}
mock_provider "null" {}

override_data {
  target = module.workspace.data.databricks_group.existing
  values = {
    id = 123
  }
}

variables {
  env_dir                   = "tests/.tmp/legacy0"
  databricks_account_id     = "account"
  databricks_client_id      = "service-principal"
  databricks_client_secret  = "secret"
  databricks_workspace_id   = "123"
  databricks_workspace_host = "https://example.invalid"
  sql_warehouse_id          = "warehouse"
  groups                    = { analysts = {} }
  genie_spaces              = [{ name = "Sales", genie_space_id = "space-1", uc_tables = ["cat.sch.customers"] }]
  genie_space_configs       = { Sales = { acl_groups = ["analysts"] } }
}

run "data_access_applied" {
  module {
    source = "../data_access/tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/legacy0/data_access/terraform.tfstate"   = jsonencode({ version = 4, outputs = { coverage_gate = { value = { fingerprint = "applied", status = "pass", max_age = "6h", table_grant_count = 1 } }, table_grant_resource_keys = { value = ["cat.sch.customers|analysts"] } } })
      "tests/.tmp/legacy0/data_access/.coverage_gate.json" = jsonencode({ status = "pass", fingerprint = "applied", refreshed_at = "@NOW@" })
    }
  }
}

run "applied_by_main" {
  state_key = "legacy"
  module {
    source = "./legacy/shared/roots/workspace"
  }
  assert {
    condition     = output.genie_exposure_blocker == ""
    error_message = "main must have opened CAN_RUN"
  }
}

run "planned_by_this_branch" {
  state_key = "legacy"
  command   = plan
}
'''


def _main_tree(tmp_path: Path) -> Path:
    import io
    import tarfile

    present = subprocess.run(["git", "cat-file", "-e", f"{BASE_COMMIT}^{{commit}}"], cwd=ROOT, capture_output=True)
    if present.returncode != 0:
        if os.environ.get("REQUIRE_TERRAFORM_TESTS") == "1":
            pytest.fail(f"base commit {BASE_COMMIT} is not in this clone; fetch full history")
        pytest.skip(f"base commit {BASE_COMMIT} is not in this clone")
    archive = subprocess.run(["git", "archive", "--format=tar", BASE_COMMIT, "shared"],
                             cwd=ROOT, capture_output=True, check=True)
    legacy = tmp_path / "legacy"
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(legacy, filter="data")
    shutil.rmtree(legacy / "shared" / "tests")
    for tests in (legacy / "shared").glob("*/*/tests"):
        shutil.rmtree(tests)
    # main's apply must not reach Databricks: no-op mask deployer and Genie script.
    (legacy / "shared" / "deploy_masking_functions.py").write_text("raise SystemExit(0)\n")
    genie = legacy / "shared" / "scripts" / "genie_space.sh"
    genie.write_text("#!/bin/sh\nexit 0\n")
    genie.chmod(0o755)
    return legacy


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform not installed")
@pytest.mark.parametrize("root_name,body", [("data_access", LEGACY_DATA_ACCESS_TEST), ("workspace", LEGACY_WORKSPACE_TEST)])
def test_legacy_roots_applied_by_main_plan_no_changes(root_name, body, tmp_path, tf_plugin_cache):
    from tests.terraform_helpers import shared_copy, tf, tf_env, tf_init

    legacy = _main_tree(tmp_path)
    copy = shared_copy(tmp_path)
    root = copy / "roots" / root_name
    # A real upgrade runs from one checkout; here the trees sit apart, so give
    # main's data_access root the same deployer path (state records it).
    main_root = legacy / "shared" / "roots" / "data_access" / "main.tf"
    text = main_root.read_text()
    assert text.count('"${local.project_root}/deploy_masking_functions.py"') == 1
    main_root.write_text(text.replace('"${local.project_root}/deploy_masking_functions.py"',
                                      json.dumps(str(copy / "deploy_masking_functions.py"))))
    (copy / "deploy_masking_functions.py").write_text("raise SystemExit(0)\n")  # scratch copy: never deploy
    shutil.copytree(legacy, root / "legacy")
    for existing in (root / "tests").glob("*.tftest.hcl"):
        existing.unlink()
    if root_name == "data_access":
        (root / "tests" / ".tmp").mkdir(exist_ok=True)
    (root / "tests" / "legacy_zero_change.tftest.hcl").write_text(body)
    env = tf_env(tmp_path, tf_plugin_cache)
    tf_init(root, env=env)
    result = tf(root, "test", "-no-color", "-verbose", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    replan = result.stdout[result.stdout.index('run "planned_by_this_branch"'):]
    assert replan.split("\n", 1)[0].endswith("pass"), replan
    assert "No changes. Your infrastructure matches the configuration." in replan, replan
