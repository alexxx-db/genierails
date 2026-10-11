#!/usr/bin/env python3
"""Live "agent tables covered" check: the gate for Genie CAN_RUN in deterministic envs.

CAN_RUN on an agent no longer rests on the data_access layer's Terraform
state. Before every workspace plan/apply of a deterministic env, make runs

    agent_coverage.py run --env-dir envs/<env> --env-name <env>

which asks Terraform (terraform console on the workspace root, same var
files and -var flags as the apply) for each agent's acl_groups and tables and
the fingerprint of those inputs, then checks Unity Catalog live against
GenieRails' own governance (design decision 9: GenieRails supports only
governance it generates):

  1. every classified column (any ``class.*`` tag) of each agent table has a
     GenieRails mask in effect: a column-mask ABAC policy on the table (or
     its schema or catalog) that is GenieRails' own by name and function
     (external_mask_inventory.is_own_policy) and whose match condition names
     one of the column's tags (read with the rulebook's condition parser). A
     table-attached mask or any other policy doesn't count; the external-mask
     inventory refuses those before rehearse, release and maintain;
  2. every acl_groups group can SELECT each table: SELECT (or ALL
     PRIVILEGES) at table, schema or catalog level plus USE CATALOG and USE
     SCHEMA, held by the group, a group it is nested in, or account users
     (the grants and SCIM membership reader_inventory.py reads), or it owns
     the table.

The result goes to envs/<env>/.agent_coverage.json: the fingerprint, the
time of the check (refreshed_at) and one pass/fail per agent with its
problems. The workspace root judges it with modules/coverage_gate_check: a
missing, unreadable, stale (inputs changed or the fingerprint edited) or
expired file withholds every new CAN_RUN group, and a failed agent gets none.
Keeping, shrinking or clearing CAN_RUN never waits on it. The file is first
marked failed, so an interrupted or failed check leaves no usable pass.

It proves the live state matches what GenieRails applied (a GenieRails mask on
every classified column, readable by every acl_groups group): masks or
grants changed outside GenieRails after the last apply are caught here.

Exit codes: 0 the check completed (``--strict``: and every agent passed),
1 ``--strict`` and an agent failed, 2 the check couldn't run (recorded failed).
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

SHARED = Path(__file__).resolve().parents[1]
for path in (SHARED, SHARED / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scripts.audit_schema_drift import _parse_condition_tag_refs  # noqa: E402
from scripts.external_mask_inventory import is_own_policy  # noqa: E402
from scripts.coverage_gate import (  # noqa: E402
    GateError, RUNNER, _run_bounded, console_flags, write_result,
)

RESULT_FILENAME = ".agent_coverage.json"
RESULT_VERSION = 1
INPUTS_EXPRESSION = (
    "base64encode(jsonencode({fingerprint = local.agent_coverage_fingerprint, "
    "inputs = local.agent_coverage_inputs}))"
)
EVERYONE = "account users"
READ = frozenset({"SELECT", "ALL PRIVILEGES"})
USE_CATALOG = frozenset({"USE CATALOG", "ALL PRIVILEGES"})
USE_SCHEMA = frozenset({"USE SCHEMA", "ALL PRIVILEGES"})
COLUMN_MASK = frozenset({"COLUMN_MASK", "POLICY_TYPE_COLUMN_MASK"})


def _privilege(value: str) -> str:
    return str(value).upper().replace("_", " ")


@dataclass
class Snapshot:
    """What Unity Catalog says about the agents' tables (live, or fake in tests)."""
    # table -> column -> {(tag key, tag value)}
    column_tags: dict[str, dict[str, set[tuple[str, str]]]] = field(default_factory=dict)
    # catalog.schema.table.column with a table-attached mask (never GenieRails')
    table_masks: set[str] = field(default_factory=set)
    # table -> effective policies: {name, function_name, policy_type, conditions: [str]}
    policies: dict[str, list[dict]] = field(default_factory=dict)
    # (securable type, name) -> {(principal, privilege)}
    grants: dict[tuple[str, str], set[tuple[str, str]]] = field(default_factory=dict)
    owners: dict[tuple[str, str], str] = field(default_factory=dict)
    # group -> direct members (users, service principals, groups)
    groups: dict[str, set[str]] = field(default_factory=dict)
    # tables that don't exist or aren't visible to the deployer
    missing: set[str] = field(default_factory=set)


def _identities(group: str, groups: Mapping[str, Iterable[str]]) -> set[str]:
    """The group, every group it is nested in (transitively), and account users."""
    found = {group}
    changed = True
    while changed:
        changed = False
        for parent, members in groups.items():
            if parent not in found and found & set(members):
                found.add(parent)
                changed = True
    return found | {EVERYONE}


def _scopes(table: str) -> tuple[tuple[str, str], tuple[str, str], tuple[str, str]]:
    catalog, schema, _ = table.split(".")
    return ("CATALOG", catalog), ("SCHEMA", f"{catalog}.{schema}"), ("TABLE", table)


def read_problem(group: str, table: str, snapshot: Snapshot) -> str | None:
    """Why ``group`` can't SELECT ``table``, or None."""
    who = _identities(group, snapshot.groups)
    catalog, schema, base = _scopes(table)

    def holds(scope: tuple[str, str], privileges: frozenset[str]) -> bool:
        return any(principal in who and _privilege(privilege) in privileges
                   for principal, privilege in snapshot.grants.get(scope, set()))

    if snapshot.owners.get(base) in who:
        return None
    missing = []
    if not (holds(catalog, USE_CATALOG) or snapshot.owners.get(catalog) in who):
        missing.append("USE CATALOG")
    if not (holds(schema, USE_SCHEMA) or holds(catalog, USE_SCHEMA) or snapshot.owners.get(schema) in who):
        missing.append("USE SCHEMA")
    if not any(holds(scope, READ) for scope in (base, schema, catalog)):
        missing.append("SELECT")
    if missing:
        return f"group {group!r} cannot read {table} (no {', '.join(missing)})"
    return None


def _policy_covers(policy: Mapping, tags: set[tuple[str, str]]) -> bool:
    if str(policy.get("policy_type") or "").upper() not in COLUMN_MASK:
        return False
    if not is_own_policy(str(policy.get("name") or ""), str(policy.get("function_name") or "")):
        return False
    keys = {key for key, _ in tags}
    for condition in policy.get("conditions") or []:
        value_refs, key_refs = _parse_condition_tag_refs(condition)
        if tags & value_refs or keys & key_refs:
            return True
    return False


def mask_problems(table: str, snapshot: Snapshot) -> list[str]:
    """Classified columns of ``table`` that no GenieRails mask covers."""
    problems = []
    for column, tags in sorted(snapshot.column_tags.get(table, {}).items()):
        classes = sorted(key for key, _ in tags if key.startswith("class."))
        if not classes:
            continue
        name = f"{table}.{column}"
        if any(_policy_covers(policy, tags) for policy in snapshot.policies.get(table, [])):
            continue
        attached = " (only a table-attached mask, which GenieRails doesn't support)" if name.lower() in {
            masked.lower() for masked in snapshot.table_masks} else ""
        problems.append(f"{name} is classified ({', '.join(classes)}) but no GenieRails mask applies to it{attached}")
    return problems


def evaluate(agents: Mapping[str, Mapping], snapshot: Snapshot) -> dict[str, dict]:
    """Per agent key: pass or fail with every problem found."""
    results = {}
    for key, agent in sorted(agents.items()):
        groups, tables = list(agent.get("groups") or []), list(agent.get("tables") or [])
        problems: list[str] = []
        if groups and not tables:
            problems.append("no tables are known for this agent; run make generate first")
        for table in tables:
            if table in snapshot.missing:
                problems.append(f"{table} does not exist or is not visible to the deployer")
                continue
            problems += mask_problems(table, snapshot)
            problems += [p for group in groups if (p := read_problem(group, table, snapshot))]
        results[key] = {
            "name": agent.get("name", key), "groups": groups, "tables": tables,
            "status": "fail" if problems else "pass", "problems": problems,
        }
    return results


# ── Inputs ───────────────────────────────────────────────────────────────────

def _load_hcl(path: Path) -> dict:
    import hcl2

    if not path.is_file():
        return {}
    return hcl2.loads(path.read_text())


def query_inputs(runner: Path, env_name: str, env_dir: Path, apply_flags: str) -> dict:
    """The workspace root's fingerprint and agent inputs (terraform console)."""
    import os

    result = _run_bounded(
        [str(runner), "workspace", env_name, "console", *console_flags(apply_flags)],
        label="terraform console (workspace agent coverage)", input=INPUTS_EXPRESSION + "\n",
        env={**os.environ, "LAYER_ENV_DIR": str(env_dir)}, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise GateError("terraform console could not evaluate the agent coverage inputs:\n"
                        + (result.stderr or result.stdout).strip())
    try:
        line = [l.strip() for l in result.stdout.splitlines() if l.strip()][-1]
        if not (line.startswith('"') and line.endswith('"')):
            raise ValueError(line)
        value = json.loads(base64.b64decode(line[1:-1], validate=True))
        value["fingerprint"], value["inputs"]["agents"]
    except (IndexError, ValueError, KeyError, TypeError) as exc:
        raise GateError(f"unexpected terraform console output: {exc}") from exc
    return value


def agent_tables(env_dir: Path, agents: Mapping[str, Mapping]) -> dict[str, dict]:
    """Add the tables make generate attributed to agents known only by ID."""
    from table_readers import _discovered_attribution, full_name

    uc_catalog = str(_load_hcl(env_dir / "env.auto.tfvars").get("uc_catalog") or "")
    discovered = _discovered_attribution(env_dir)
    resolved = {}
    for key, agent in agents.items():
        tables = list(agent.get("tables") or []) or discovered.get(str(agent.get("name")), [])
        resolved[key] = {**agent, "tables": sorted({full_name(t, uc_catalog) for t in tables if t})}
    return resolved


# ── Live source ──────────────────────────────────────────────────────────────

class LiveSnapshot:
    """Reads a Snapshot from the workspace (SDK and SQL warehouse)."""

    def __init__(self, env_dir: Path):
        from scripts.audit_schema_drift import _get_sdk_client, _get_warehouse_id
        from reader_inventory import DatabricksSource

        self.client = _get_sdk_client(env_dir)
        warehouse = _get_warehouse_id(env_dir, self.client)
        if not warehouse:
            raise RuntimeError("no SQL warehouse is available for the agent coverage check")
        self.source = DatabricksSource(warehouse, client=self.client)

    def expand(self, tables: Iterable[str]) -> list[str]:
        """Concrete tables: catalog.schema.* patterns listed live."""
        result = set()
        for table in tables:
            if not table.endswith(".*"):
                result.add(table.lower())
                continue
            catalog, schema, _ = table.split(".")
            rows = self.source._query(
                "SELECT table_catalog, table_schema, table_name FROM system.information_schema.tables "
                f"WHERE table_catalog = {self.source._literal(catalog)} AND table_schema = {self.source._literal(schema)}")
            result.update(".".join(map(str, row[:3])).lower() for row in rows)
        return sorted(result)

    def _policies(self, table: str) -> list[dict]:
        rows = []
        for policy in self.client.policies.list_policies("TABLE", table, include_inherited=True):
            policy_type = getattr(policy, "policy_type", "")
            mask = getattr(policy, "column_mask", None)
            on_column = getattr(mask, "on_column", None) if mask else None
            matches = list(getattr(policy, "match_columns", None) or [])
            chosen = [m for m in matches if on_column and getattr(m, "alias", None) == on_column] or matches
            rows.append({
                "name": policy.name,
                "function_name": getattr(mask, "function_name", "") if mask else "",
                "policy_type": getattr(policy_type, "value", policy_type),
                "conditions": [str(getattr(m, "condition", "") or "") for m in chosen],
            })
        return rows

    def read(self, tables: Iterable[str]) -> Snapshot:
        tables = sorted(set(tables))
        snapshot = Snapshot(groups={k: set(v) for k, v in self.source.group_members().items()})
        for table in tables:
            catalog, schema, base = _scopes(table)
            if not self.source.owner(*base):
                snapshot.missing.add(table)
                continue
            for scope in (catalog, schema, base):
                if scope not in snapshot.owners:
                    owner = self.source.owner(*scope)
                    if owner:
                        snapshot.owners[scope] = owner
                    snapshot.grants[scope] = {
                        (grant.principal, grant.privilege) for grant in self.source.grants(*scope)}
            snapshot.policies[table] = self._policies(table)
        present = [t for t in tables if t not in snapshot.missing]
        if present:
            listed = ", ".join(self.source._literal(t) for t in present)
            for row in self.source._query(
                    "SELECT catalog_name, schema_name, table_name, column_name, tag_name, tag_value "
                    "FROM system.information_schema.column_tags WHERE lower(concat_ws('.', catalog_name, "
                    f"schema_name, table_name)) IN ({listed})"):
                table = ".".join(map(str, row[:3])).lower()
                snapshot.column_tags.setdefault(table, {}).setdefault(str(row[3]), set()).add(
                    (str(row[4]), str(row[5] or "")))
            for row in self.source._query(
                    "SELECT table_catalog, table_schema, table_name, column_name FROM "
                    "system.information_schema.column_masks WHERE lower(concat_ws('.', table_catalog, "
                    f"table_schema, table_name)) IN ({listed})"):
                snapshot.table_masks.add(".".join(map(str, row[:4])).lower())
        return snapshot


# ── Commands ─────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def invalidate(env_dir: Path, reason: str) -> None:
    """Record a failed result, so nothing older can open CAN_RUN."""
    path = env_dir / RESULT_FILENAME
    try:
        record = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(record, dict):
            record = {}
    except (OSError, ValueError):
        record = {}
    record.update(version=RESULT_VERSION, status="fail", reason=reason, agents={})
    record.pop("refreshed_at", None)
    write_result(path, record)


def run(env_dir: Path, env_name: str, runner: Path, apply_flags: str, *, strict: bool = False,
        snapshot_factory: Callable[[Path], object] = LiveSnapshot) -> int:
    from table_readers import deterministic

    env_dir = env_dir.resolve()
    cfg = _load_hcl(env_dir / "env.auto.tfvars")
    if not deterministic(cfg):
        return 0
    path = env_dir / RESULT_FILENAME
    invalidate(env_dir, "the live agent coverage check started and has not finished")
    try:
        value = query_inputs(runner, env_name, env_dir, apply_flags)
        agents = agent_tables(env_dir, value["inputs"]["agents"])
        needed = {key: agent for key, agent in agents.items() if agent.get("groups")}
        if needed:
            live = snapshot_factory(env_dir)
            expanded = {key: {**agent, "tables": live.expand(agent["tables"])} for key, agent in needed.items()}
            snapshot = live.read({t for agent in expanded.values() for t in agent["tables"]})
            results = evaluate(expanded, snapshot)
        else:
            results = {}
        for key, agent in agents.items():
            results.setdefault(key, {**agent, "status": "pass", "problems": []})
    except Exception as exc:  # anything incomplete is a failure, never an empty pass
        invalidate(env_dir, f"the live agent coverage check failed: {exc}")
        print(f"WARNING: live agent coverage check for workspace:{env_name} could not run: {exc}\n"
              "  New Genie CAN_RUN is withheld until it passes; keeping, shrinking or clearing "
              "CAN_RUN still applies.", file=sys.stderr)
        return 2
    write_result(path, {
        "version": RESULT_VERSION, "env": env_name,
        # "pass" = the check completed; each agent carries its own verdict.
        "status": "pass", "fingerprint": value["fingerprint"],
        "refreshed_at": _now(), "agents": results,
    })
    print(f"=== Live agent coverage check (workspace:{env_name}) ===")
    failed = {key: r for key, r in results.items() if r["status"] != "pass"}
    for key, result in sorted(results.items()):
        if result.get("groups"):
            print(f"  {result['name']}: {result['status']} ({len(result['tables'])} table(s); "
                  f"acl_groups {', '.join(result['groups'])})")
    for key, result in sorted(failed.items()):
        for problem in result["problems"]:
            print(f"  - {result['name']}: {problem}", file=sys.stderr)
    if failed:
        print(f"WARNING: new Genie CAN_RUN is withheld for {', '.join(r['name'] for r in failed.values())} "
              "until every classified column of its tables is masked and every acl_groups group can "
              "read them (above).", file=sys.stderr)
    return 1 if strict and failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("run", help="run the live check and record the result for the workspace layer")
    command.add_argument("--env-dir", required=True, type=Path)
    command.add_argument("--env-name", required=True)
    command.add_argument("--runner", type=Path, default=RUNNER)
    command.add_argument("--apply-flags", default="")
    command.add_argument("--strict", action="store_true", help="exit 1 when any agent fails (maintain)")
    stale = sub.add_parser("invalidate", help="mark the recorded result failed")
    stale.add_argument("--env-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "invalidate":
        invalidate(args.env_dir, "invalidated")
        return 0
    return run(args.env_dir, args.env_name, args.runner, args.apply_flags, strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
