#!/usr/bin/env python3
"""Refuse a run while a mask GenieRails didn't create can hit a governed column.

Two column masks resolving on one column fail every query on it with
MULTIPLE_MASKS. Before every rehearse, release and maintain of a deterministic
env this inventories, on each governed table:

* table-attached masks (``ALTER TABLE ... ALTER COLUMN ... SET MASK``) on a
  governed column, from ``system.information_schema.column_masks``. GenieRails
  never attaches one, so every such mask is external;
* effective ABAC column-mask policies (catalog, schema and table level), from
  the Unity Catalog policies API. A policy is GenieRails' own only when both its
  name and its function follow the deterministic naming in
  governance_policies.py, so retained generations a staged change keeps alive
  are recognised too. Any other column-mask policy on a governed table stops
  the run: its match condition depends on tags GenieRails doesn't own, so it
  can't be proven to miss the governed columns. The one exception is a legacy
  GenieRails mask this env's own data_access state manages: a migration from
  legacy governance (scripts/migration.py) keeps it live until it retires.

Exit codes: 0 clean, 1 external masks found, 2 the inventory couldn't be read.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SHARED = Path(__file__).resolve().parents[1]
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from governance_policies import TAG_KEY, parse_treatment_value  # noqa: E402

_OWN_POLICY = re.compile(r"(?P<catalog>.+)_gr_mask_(?P=catalog)_(?P<value>.+)_(?:partial|full)")
_OWN_FUNCTION = re.compile(r"(?:[^.]+\.){2}gr_mask_(?P<value>.+)_(?:partial|full)")


def is_own_policy(name: str, function_name: str) -> bool:
    """Whether a live column-mask policy is one deterministic governance created."""
    policy = _OWN_POLICY.fullmatch(name or "")
    function = _OWN_FUNCTION.fullmatch(function_name or "")
    return bool(
        policy and function and policy["value"] == function["value"]
        and parse_treatment_value(policy["value"])
    )


def external_masks(
    governed_columns: set[str], direct_rows: list[list[str]], policies_by_table: dict[str, list[dict]],
    own_legacy: set[str] = frozenset(),
) -> list[str]:
    """Every external mask that could resolve on a governed column, described."""
    governed = {column.lower() for column in governed_columns}
    problems: set[str] = set()
    for row in direct_rows:
        if len(row) != 5 or any(not isinstance(value, str) or not value for value in row):
            raise ValueError(
                "unexpected information_schema.column_masks row; expected five non-empty "
                "strings: table_catalog, table_schema, table_name, column_name, mask_name"
            )
        column = ".".join(str(value) for value in row[:4])
        if len(row[4].split(".")) != 3 or any(not part for part in row[4].split(".")):
            raise ValueError(
                "unexpected information_schema.column_masks mask_name; expected "
                "a fully qualified catalog.schema.function name"
            )
        if column.lower() in governed:
            mask = row[4]
            problems.add(f"{column}: table-attached mask {mask}")
    for table, policies in sorted(policies_by_table.items()):
        for policy in policies:
            if str(policy.get("policy_type") or "").upper() not in {"COLUMN_MASK", "POLICY_TYPE_COLUMN_MASK"}:
                continue
            name = str(policy.get("name") or "")
            function = str(policy.get("function_name") or "")
            if not is_own_policy(name, function) and name not in own_legacy:
                where = policy.get("on_securable_fullname") or table
                problems.add(f"{table}: column-mask policy {name!r} on {where} (function {function or '?'})")
    return sorted(problems)


def governed_columns_and_tables(env_dir: Path) -> tuple[set[str], list[str]]:
    from scripts.audit_schema_drift import _load_hcl
    from scripts.sticky_governance import load_governed_tables

    from scripts.runtime_overlay import effective

    cfg = _load_hcl(effective(env_dir, "abac.auto.tfvars"))
    columns = {
        str(item.get("entity_name")) for item in cfg.get("tag_assignments") or []
        if item.get("entity_type") == "columns" and item.get("tag_key") == TAG_KEY
    }
    tables = {".".join(column.split(".")[:3]) for column in columns}
    tables.update(load_governed_tables(env_dir))
    return columns, sorted(tables)


def _policy_rows(w, table: str) -> list[dict]:
    rows = []
    for policy in w.policies.list_policies("TABLE", table, include_inherited=True):
        mask = getattr(policy, "column_mask", None)
        policy_type = getattr(policy, "policy_type", "")
        rows.append({
            "name": policy.name,
            "policy_type": getattr(policy_type, "value", policy_type),
            "on_securable_fullname": policy.on_securable_fullname,
            "function_name": getattr(mask, "function_name", "") if mask else "",
        })
    return rows


def inventory(env_dir: Path) -> list[str]:
    from scripts.audit_schema_drift import _get_sdk_client, _get_warehouse_id, _run_sql

    columns, tables = governed_columns_and_tables(env_dir)
    if not tables:
        return []
    w = _get_sdk_client(env_dir)
    direct: list[list[str]] = []
    if columns:
        warehouse = _get_warehouse_id(env_dir, w)
        if not warehouse:
            raise RuntimeError("no SQL warehouse is available for the external-mask inventory")
        quoted = ",".join("'" + value.lower().replace("'", "''") + "'" for value in sorted(columns))
        direct = _run_sql(
            w, warehouse,
            "SELECT table_catalog, table_schema, table_name, column_name, mask_name "
            "FROM system.information_schema.column_masks WHERE lower(concat_ws('.', table_catalog, "
            f"table_schema, table_name, column_name)) IN ({quoted})",
        )
    return external_masks(columns, direct, {table: _policy_rows(w, table) for table in tables},
                          legacy_policy_names(env_dir))


def legacy_policy_names(env_dir: Path) -> set[str]:
    """Live names of the legacy GenieRails masks this env's data_access state manages."""
    from scripts.migration import deployment

    return {f"{p['catalog']}_{p['name']}" for p in deployment(env_dir)["masks"].values()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        problems = inventory(args.env_dir)
    except Exception as exc:
        print(f"ERROR: external-mask inventory failed: {exc}", file=sys.stderr)
        return 2
    if problems:
        print("ERROR: a mask GenieRails didn't create can resolve on a governed column; "
              "queries on it would fail with MULTIPLE_MASKS. Remove it before continuing:",
              file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("External-mask inventory passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
