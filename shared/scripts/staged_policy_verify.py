#!/usr/bin/env python3
"""Live check of one staged policy phase: what Unity Catalog enforces is the render.

Run by scripts/staged_policy_apply.py after each staged apply. It reads the
stage the last hook applied (``.policy_generations/stage.json``) and compares
it with what is live:

* every rendered column-mask policy is live on its catalog with exactly the
  rendered ``TO`` and ``EXCEPT`` principals, function and match condition, and
  no other GenieRails mask policy is live there (the policies API);
* the effective policies on every governed table (``include_inherited``,
  catalog, schema and table level) hold no mask GenieRails didn't render;
* every function a rendered policy calls exists;
* every rendered column carries exactly its rendered treatment value, and a
  column the render leaves untagged carries none
  (``system.information_schema.column_tags``).

Together with the render check of the hooks (every tagged value has a policy,
nobody gets two masks), this proves no governed column is unmasked or doubly
masked at this stage. verify-access then proves the outputs per tier.

Exit codes: 0 match, 1 a difference, 2 the live state couldn't be read.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SHARED = Path(__file__).resolve().parents[1]
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from scripts import policy_generations as pg  # noqa: E402
from scripts.external_mask_inventory import is_own_policy  # noqa: E402


def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    value = getattr(obj, key, default)
    return getattr(value, "value", value) if key == "policy_type" else value


def normalize_policy(policy) -> dict:
    """A live policy (SDK object or API dict) as the fields this check compares."""
    mask = _get(policy, "column_mask") or {}
    return {
        "name": str(_get(policy, "name") or ""),
        "policy_type": str(_get(policy, "policy_type") or ""),
        "to": sorted(_get(policy, "to_principals") or []),
        "except": sorted(_get(policy, "except_principals") or []),
        "function": str(_get(mask, "function_name") or "").lower(),
        "conditions": sorted(re.sub(r"\s+", "", str(_get(m, "condition") or ""))
                             for m in _get(policy, "match_columns") or []),
        "on": str(_get(policy, "on_securable_fullname") or ""),
    }


def _is_mask(policy: dict) -> bool:
    return policy["policy_type"].upper() in {"COLUMN_MASK", "POLICY_TYPE_COLUMN_MASK"}


def expected_policies(rendered: dict) -> dict[str, dict]:
    """Live name -> the fields a rendered mask policy must have live."""
    out = {}
    for policy in rendered["fgac_policies"]:
        out[f"{policy['catalog']}_{policy['name']}"] = {
            "catalog": policy["catalog"],
            "to": sorted(policy.get("to_principals") or []),
            "except": sorted(policy.get("except_principals") or []),
            "function": f"{policy['function_catalog']}.{policy['function_schema']}.{policy['function_name']}".lower(),
            "condition": re.sub(r"\s+", "", str(policy.get("match_condition") or "")),
        }
    return out


def rendered_functions(rendered: dict) -> set[str]:
    """Fully qualified (lowercase) functions the render's policies call."""
    return {p["function"] for p in expected_policies(rendered).values()}


def live_problems(rendered: dict, catalog_policies: dict[str, list], table_policies: dict[str, list],
                  live_functions: set[str], live_tags: dict[str, str], untagged: set[str]) -> list[str]:
    """Every difference between the render and what is live (pure)."""
    problems = []
    expected = expected_policies(rendered)
    for catalog, policies in sorted(catalog_policies.items()):
        live = {p["name"]: p for p in map(normalize_policy, policies) if _is_mask(p)}
        for name, want in sorted(expected.items()):
            if want["catalog"] != catalog:
                continue
            have = live.get(name)
            if have is None:
                problems.append(f"policy {name} is not live on catalog {catalog}")
                continue
            for field in ("to", "except", "function"):
                if have[field] != want[field]:
                    problems.append(f"policy {name}: live {field} {have[field]}, rendered {want[field]}")
            if want["condition"] not in have["conditions"]:
                problems.append(f"policy {name}: live match {have['conditions']}, rendered {want['condition']}")
        for name, have in sorted(live.items()):
            if name not in expected and is_own_policy(name, have["function"]):
                problems.append(f"policy {name} is live on catalog {catalog} but not in this stage's render")
    for table, policies in sorted(table_policies.items()):
        for have in map(normalize_policy, policies):
            if _is_mask(have) and have["name"] not in expected:
                problems.append(f"{table}: effective mask policy {have['name']} on {have['on'] or table} "
                                "is not in this stage's render")
    for function in sorted(rendered_functions(rendered) - {f.lower() for f in live_functions}):
        problems.append(f"function {function} doesn't exist")
    for column, value in sorted(rendered["tags"].items()):
        have = live_tags.get(column.lower())
        if have != value:
            problems.append(f"{column}: live treatment {have or 'none'}, rendered {value}")
    for column in sorted(untagged):
        if live_tags.get(column.lower()):
            problems.append(f"{column}: live treatment {live_tags[column.lower()]}, rendered none")
    return problems


def _columns_of(env_dir: Path, record: dict) -> set[str]:
    columns = set(record["rendered"]["tags"])
    for key in ("desired", "old", "retire"):
        if record.get(key):
            columns.update((pg.load_snapshot(env_dir, record[key]) or {}).get("tags") or {})
    return columns


def read_live(env_dir: Path, rendered: dict, columns: set[str]):
    """Live policies, functions and treatment tags, through the deployer's credentials."""
    from scripts.audit_schema_drift import _get_sdk_client, _get_warehouse_id, _run_sql

    w = _get_sdk_client(env_dir / "data_access")
    catalogs = {p["catalog"] for p in rendered["fgac_policies"]} | {c.split(".")[0] for c in columns}
    catalog_policies = {c: list(w.policies.list_policies("CATALOG", c)) for c in sorted(catalogs)}
    tables = sorted({".".join(c.split(".")[:3]) for c in columns})
    table_policies = {t: list(w.policies.list_policies("TABLE", t, include_inherited=True)) for t in tables}
    functions = set()
    for name in sorted(rendered_functions(rendered)):
        try:
            w.functions.get(name)
            functions.add(name)
        except Exception as exc:  # NotFound is the expected miss; anything else is unreadable
            if "not" not in str(exc).lower() or "found" not in str(exc).lower():
                raise
    tags: dict[str, str] = {}
    if columns:
        warehouse = _get_warehouse_id(env_dir, w)
        if not warehouse:
            raise RuntimeError("no SQL warehouse is available to read live column tags")
        quoted = ",".join("'" + c.lower().replace("'", "''") + "'" for c in sorted(columns))
        for row in _run_sql(
            w, warehouse,
            "SELECT concat_ws('.', catalog_name, schema_name, table_name, column_name), tag_value "
            f"FROM system.information_schema.column_tags WHERE tag_name = '{pg.TAG_KEY}' AND "
            f"lower(concat_ws('.', catalog_name, schema_name, table_name, column_name)) IN ({quoted})",
        ):
            tags[str(row[0]).lower()] = str(row[1] or "")
    return catalog_policies, table_policies, functions, tags


def verify(env_dir: Path, stages: set[str], *, reader=read_live) -> list[str]:
    record = pg.load_stage_record(env_dir)
    if not record:
        return ["no staged apply is recorded"]
    if record["stage"] not in stages:
        return [f"the last applied stage is {record['stage']}, expected {'/'.join(sorted(stages))}"]
    rendered = record["rendered"]
    columns = _columns_of(env_dir, record)
    catalog_policies, table_policies, functions, tags = reader(env_dir, rendered, columns)
    return live_problems(rendered, catalog_policies, table_policies, functions, tags,
                         columns - set(rendered["tags"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-dir", type=Path, required=True)
    parser.add_argument("--stage", required=True, help="comma-separated stages the last apply may be")
    args = parser.parse_args(argv)
    stages = set(args.stage.split(","))
    try:
        problems = verify(args.env_dir.resolve(), stages)
    except Exception as exc:
        print(f"ERROR: the live policy check couldn't read Unity Catalog: {exc}", file=sys.stderr)
        return 2
    if problems:
        print(f"ERROR: live governance doesn't match the {'/'.join(sorted(stages))} stage:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"Live policy check ({'/'.join(sorted(stages))}): policies, functions and treatment tags match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
