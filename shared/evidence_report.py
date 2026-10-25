#!/usr/bin/env python3
"""Build versioned JSON and Markdown compliance evidence for a GenieRails footprint.

Offline mode (the default) uses local configuration only and never imports the
Databricks SDK. Set GENIERAILS_EVIDENCE_INTEGRATION=1 to collect live Unity
Catalog state through a Databricks SQL warehouse.

Live mode reads two kinds of mask: table-attached masks and row filters from
information_schema (legacy envs), and the ABAC policies the policies API lists
as effective on each governed table (catalog, schema and table level), which is
how deterministic governance masks columns. A column carries every ABAC column
mask whose match condition its tags satisfy, with the policy's TO and EXCEPT
principals. A policy listing that fails, or a policy this report can't evaluate,
stops the run (exit 2) rather than writing a report that claims no protection.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from scripts.footprint import resolve_footprint

SCHEMA_VERSION = "1.0"
INTEGRATION_ENV = "GENIERAILS_EVIDENCE_INTEGRATION"
COLUMN_MASK_TYPES = {"COLUMN_MASK", "POLICY_TYPE_COLUMN_MASK"}
ROW_FILTER_TYPES = {"ROW_FILTER", "POLICY_TYPE_ROW_FILTER"}
# What may remain of a match condition once its tag references are removed for
# "any reference matches" to be its exact meaning: OR, parentheses, whitespace.
_TAG_REF = re.compile(r"hasTagValue\(\s*'[^']+'\s*,\s*'[^']+'\s*\)|hasTag\(\s*'[^']+'\s*\)")
_ONLY_OR = re.compile(r"(?i)[\s()]*(?:\bOR\b[\s()]*)*")


class EvidenceUnavailable(RuntimeError):
    """Live protection state couldn't be read completely, so no report is written."""


def _utc(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fqn(row: dict[str, Any]) -> str:
    return ".".join(str(row[k]) for k in ("catalog", "schema", "table", "column"))


def assemble_report(
    columns: Iterable[dict[str, Any]],
    *,
    classifications: Iterable[dict[str, Any]] = (),
    tags: Iterable[dict[str, Any]] = (),
    masks: Iterable[dict[str, Any]] = (),
    policies: Iterable[dict[str, Any]] = (),
    abac_policies: Iterable[dict[str, Any]] = (),
    grants: Iterable[dict[str, Any]] = (),
    generated_at: datetime | None = None,
    approved_by: str = "unapproved",
    approved_at: str | None = None,
    source: str = "offline-config",
) -> dict[str, Any]:
    """Join normalized state rows into a stable, auditable report."""
    tag_map: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in tags:
        tag_map[_fqn(row)].append({"name": str(row["name"]), "value": str(row.get("value", ""))})
    mask_map = {_fqn(row): str(row["name"]) for row in masks}
    policy_map: dict[str, list[str]] = defaultdict(list)
    for row in policies:
        policy_map[_fqn(row)].append(str(row["name"]))
    abac_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in abac_policies:
        abac_map[_fqn(row)].append({
            "name": str(row["name"]),
            "policy_type": str(row["policy_type"]),
            "on_securable": str(row.get("on_securable", "")),
            "function": str(row.get("function", "")),
            "match_conditions": sorted(str(item) for item in row.get("match_conditions", [])),
            "when_condition": str(row.get("when_condition", "")),
            "to_principals": sorted(str(item) for item in row.get("to_principals", [])),
            "except_principals": sorted(str(item) for item in row.get("except_principals", [])),
        })
        policy_map[_fqn(row)].append(str(row["name"]))
    class_map = {_fqn(row): row for row in classifications}
    grant_map: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in grants:
        grant_map[_fqn(row)].append({
            "principal": str(row["principal"]),
            "privilege": str(row["privilege"]),
            "scope": str(row.get("scope", "COLUMN")),
        })

    evidence = []
    for column in sorted(columns, key=_fqn):
        key = _fqn(column)
        scan = class_map.get(key, {})
        evidence.append({
            "catalog": str(column["catalog"]),
            "schema": str(column["schema"]),
            "table": str(column["table"]),
            "column": str(column["column"]),
            "classification": {
                "status": str(scan.get("status", "not_run")),
                "scanned_at": scan.get("scanned_at"),
            },
            "detected_tags": sorted(tag_map.get(key, []), key=lambda item: (item["name"], item["value"])),
            "applied_mask": mask_map.get(key),
            "applied_policies": sorted(set(policy_map.get(key, []))),
            "applied_abac_policies": sorted(abac_map.get(key, []), key=lambda item: (item["name"], item["on_securable"])),
            "grants": sorted(grant_map.get(key, []), key=lambda item: (item["principal"], item["privilege"], item["scope"])),
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "report_type": "genierails.compliance_evidence",
        "header": {
            "generated_at": _utc(generated_at),
            "approved_by": approved_by,
            "approved_at": approved_at,
            "source": source,
        },
        "evidence": evidence,
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Render a compact human-readable view of the canonical JSON artifact."""
    header = report["header"]
    lines = [
        "# GenieRails Compliance Evidence",
        "",
        f"- Schema version: `{report['schema_version']}`",
        f"- Generated at: `{header['generated_at']}`",
        f"- Source: `{header['source']}`",
        f"- Approved by: `{header['approved_by']}`",
        f"- Approved at: `{header['approved_at'] or 'pending'}`",
    ]
    if "column_privileges_availability" in report:
        availability = report["column_privileges_availability"]
        detail = ", ".join(f"{catalog}={status}" for catalog, status in sorted(availability.items())) or "not queried"
        lines.append(f"- Column privileges: `{detail}`")
    if "abac_policy_listing" in report:
        listing = report["abac_policy_listing"]
        lines.append(f"- ABAC policies: `{listing['status']}` ({listing['policies']} effective on {listing['tables']} tables)")
    lines.extend([
        "", "| Table | Column | Scan status | Scanned at | Tags | Applied mask / policies | Grants |",
        "|---|---|---|---|---|---|---|",
    ])
    for item in report["evidence"]:
        table = ".".join((item["catalog"], item["schema"], item["table"]))
        tags = ", ".join(f"{tag['name']}={tag['value']}" for tag in item["detected_tags"]) or "—"
        grants = ", ".join(
            f"{grant['principal']}:{grant['privilege']} ({grant['scope']})" for grant in item["grants"]
        ) or "—"
        abac = item.get("applied_abac_policies", [])
        abac_names = {policy["name"] for policy in abac}
        protections = ", ".join(
            ([item["applied_mask"]] if item["applied_mask"] else [])
            + [name for name in item["applied_policies"] if name not in abac_names]
            + [f"{policy['name']} (to: {', '.join(policy['to_principals']) or 'none'}; "
               f"except: {', '.join(policy['except_principals']) or 'none'})" for policy in abac]
        ) or "—"
        values = [table, item["column"], item["classification"]["status"],
                  item["classification"]["scanned_at"] or "—", tags,
                  protections, grants]
        lines.append("| " + " | ".join(str(value).replace("|", "\\|") for value in values) + " |")
    return "\n".join(lines) + "\n"


def _load_hcl(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import hcl2
    except ImportError as exc:
        raise RuntimeError("python-hcl2 is required; install it with: pip install python-hcl2") from exc
    with path.open(encoding="utf-8") as handle:
        return hcl2.load(handle)


def configured_columns(env_dir: Path) -> list[dict[str, str]]:
    """Return column footprint declared by environment tag assignments."""
    configs = [env_dir / "data_access" / "abac.auto.tfvars", env_dir / "abac.auto.tfvars"]
    found: dict[str, dict[str, str]] = {}
    for config in configs:
        for assignment in _load_hcl(config).get("tag_assignments", []):
            if assignment.get("entity_type") != "columns":
                continue
            parts = str(assignment.get("entity_name", "")).split(".")
            if len(parts) == 4:
                row = dict(zip(("catalog", "schema", "table", "column"), parts))
                found[_fqn(row)] = row
    return list(found.values())


def configured_tags(env_dir: Path) -> list[dict[str, str]]:
    rows = []
    for config in (env_dir / "data_access" / "abac.auto.tfvars", env_dir / "abac.auto.tfvars"):
        for item in _load_hcl(config).get("tag_assignments", []):
            parts = str(item.get("entity_name", "")).split(".")
            if item.get("entity_type") == "columns" and len(parts) == 4:
                row = dict(zip(("catalog", "schema", "table", "column"), parts))
                row.update(name=str(item.get("tag_key", "")), value=str(item.get("tag_value", "")))
                rows.append(row)
    return rows


def configured_tables(env_dir: Path) -> list[str]:
    """Return the tables managed by configured Genie agents."""
    tables = set(resolve_footprint(env_dir))
    # Tag assignments remain a useful fallback for governance-only environments.
    tables.update(".".join(_fqn(row).split(".")[:3]) for row in configured_columns(env_dir))
    return sorted(table for table in tables if len(table.split(".")) == 3)


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_identifier(value: str) -> str:
    return "`" + value.replace("`", "``") + "`"


def _target_conditions(policy: Any) -> list[str]:
    """The match conditions of the column a policy masks (agent_coverage's choice)."""
    from scripts.staged_policy_verify import _get

    mask = _get(policy, "column_mask")
    on_column = _get(mask, "on_column") if mask else None
    matches = list(_get(policy, "match_columns") or [])
    chosen = [m for m in matches if on_column and _get(m, "alias") == on_column] or matches
    return [str(_get(m, "condition") or "") for m in chosen]


def abac_policy_rows(table: str, policies: Iterable[Any], columns: list[dict[str, Any]],
                     column_tags: dict[str, set[tuple[str, str]]]) -> list[dict[str, Any]]:
    """One row per (column, effective ABAC policy that applies to it) on ``table``.

    A column mask applies to a column whose tags satisfy one of its match
    conditions (hasTagValue/hasTag, parsed as audit-rulebook and agent coverage
    do); a row filter applies to every column of the table. Anything this can't
    evaluate exactly raises EvidenceUnavailable instead of being dropped.
    """
    from scripts.audit_schema_drift import _parse_condition_tag_refs
    from scripts.staged_policy_verify import _get, normalize_policy

    rows = []
    for policy in policies:
        live = normalize_policy(policy)
        kind = live["policy_type"].upper()
        if not live["name"] or kind not in COLUMN_MASK_TYPES | ROW_FILTER_TYPES:
            raise EvidenceUnavailable(
                f"{table}: effective policy {live['name'] or '(unnamed)'} has policy type "
                f"{live['policy_type'] or '(none)'}, which this report can't evaluate")
        record = {
            "name": live["name"], "policy_type": "COLUMN_MASK" if kind in COLUMN_MASK_TYPES else "ROW_FILTER",
            "on_securable": live["on"] or table, "function": live["function"],
            "match_conditions": [str(_get(m, "condition") or "") for m in _get(policy, "match_columns") or []],
            "when_condition": str(_get(policy, "when_condition") or ""),
            "to_principals": live["to"], "except_principals": live["except"],
        }
        if kind in ROW_FILTER_TYPES:
            rows.extend({**column, **record} for column in columns)
            continue
        conditions = _target_conditions(policy)
        if not live["function"] or not conditions or any(
                not condition.strip() or _ONLY_OR.fullmatch(_TAG_REF.sub(" ", condition)) is None
                for condition in conditions):
            raise EvidenceUnavailable(
                f"{table}: column-mask policy {live['name']} has a function or match condition "
                "this report can't evaluate exactly")
        value_refs: set[tuple[str, str]] = set()
        key_refs: set[str] = set()
        for condition in conditions:
            values, keys = _parse_condition_tag_refs(condition)
            value_refs |= values
            key_refs |= keys
        for column in columns:
            tags = column_tags.get(_fqn(column), set())
            if tags & value_refs or {key for key, _ in tags} & key_refs:
                rows.append({**column, **record})
    return rows


def collect_live_state(columns: list[dict[str, str]], tables: list[str], warehouse_id: str,
                       auth_file: Path = Path("auth.auto.tfvars")) -> dict[str, Any]:
    """Read live state through the SDK; called only in explicitly enabled integration mode."""
    from scripts.auth_configured import workspace_client
    client = workspace_client(auth_file, product="genierails-evidence", product_version=SCHEMA_VERSION)
    if not tables:
        return {name: [] for name in ("columns", "classifications", "tags", "masks", "policies", "grants")}
    def query(sql: str) -> list[list[Any]]:
        response = client.statement_execution.execute_statement(
            statement=sql, warehouse_id=warehouse_id, wait_timeout="50s"
        )
        state = str(getattr(getattr(response, "status", None), "state", ""))
        while state.endswith(("PENDING", "RUNNING")):
            time.sleep(1)
            response = client.statement_execution.get_statement(response.statement_id)
            state = str(getattr(getattr(response, "status", None), "state", ""))
        if state.endswith("FAILED"):
            error = getattr(getattr(response, "status", None), "error", None)
            raise RuntimeError(getattr(error, "message", "Databricks SQL statement failed"))
        return list(getattr(getattr(response, "result", None), "data_array", None) or [])

    table_key_sql = ", ".join(_sql_literal(key) for key in tables)
    live_columns = query("SELECT table_catalog, table_schema, table_name, column_name FROM system.information_schema.columns WHERE concat(table_catalog, '.', table_schema, '.', table_name) IN (" + table_key_sql + ")")
    live_column_dicts = [dict(zip(("catalog", "schema", "table", "column"), row[:4])) for row in live_columns]
    keys = ", ".join(_sql_literal(_fqn(row)) for row in live_column_dicts)
    if not keys:
        return {name: [] for name in ("columns", "classifications", "tags", "masks", "policies", "grants")}
    predicate = "concat(table_catalog, '.', table_schema, '.', table_name, '.', column_name) IN (" + keys + ")"
    tag_rows = query("SELECT catalog_name, schema_name, table_name, column_name, tag_name, tag_value FROM system.information_schema.column_tags WHERE concat(catalog_name, '.', schema_name, '.', table_name, '.', column_name) IN (" + keys + ")")
    # Azure's live view has no mask catalog/schema columns, so mask_name must
    # remain bare; table_catalog/table_schema identify the protected table.
    mask_rows = query("SELECT table_catalog, table_schema, table_name, column_name, mask_name FROM system.information_schema.column_masks WHERE concat(table_catalog, '.', table_schema, '.', table_name, '.', column_name) IN (" + keys + ")")
    # COLUMN_PRIVILEGES is optional in Databricks information_schema. Discover it
    # per catalog rather than assuming the system-wide relation exists (Azure
    # workspaces commonly expose no column-level privilege relation at all).
    grant_rows = []
    column_privileges_availability = {}
    for catalog in sorted({key.split(".")[0] for key in tables}):
        information_schema = _sql_identifier(catalog) + ".information_schema"
        relations = query("SHOW TABLES IN " + information_schema)
        available = any(len(row) > 1 and str(row[1]).lower() == "column_privileges" for row in relations)
        column_privileges_availability[catalog] = "available" if available else "not_available"
        if available:
            grant_rows.extend(query("SELECT table_catalog, table_schema, table_name, column_name, grantee, privilege_type FROM " + information_schema + ".column_privileges WHERE " + predicate))
    table_grant_rows = query("SELECT table_catalog, table_schema, table_name, grantee, privilege_type FROM system.information_schema.table_privileges WHERE concat(table_catalog, '.', table_schema, '.', table_name) IN (" + table_key_sql + ")")
    schema_keys = sorted({".".join(key.split(".")[:2]) for key in tables})
    catalog_keys = sorted({key.split(".")[0] for key in tables})
    schema_grant_rows = query("SELECT catalog_name, schema_name, grantee, privilege_type FROM system.information_schema.schema_privileges WHERE concat(catalog_name, '.', schema_name) IN (" + ", ".join(_sql_literal(key) for key in schema_keys) + ")")
    catalog_grant_rows = query("SELECT catalog_name, grantee, privilege_type FROM system.information_schema.catalog_privileges WHERE catalog_name IN (" + ", ".join(_sql_literal(key) for key in catalog_keys) + ")")
    # Azure's live view likewise has no filter catalog/schema columns.
    policy_rows = query("SELECT table_catalog, table_schema, table_name, filter_name FROM system.information_schema.row_filters WHERE concat(table_catalog, '.', table_schema, '.', table_name) IN (" + table_key_sql + ")")

    base = lambda row: dict(zip(("catalog", "schema", "table", "column"), row[:4]))
    result: dict[str, Any] = {
        "columns": live_column_dicts, "classifications": [],
        "tags": [], "masks": [], "policies": [], "grants": [],
    }
    result["column_privileges_availability"] = column_privileges_availability
    for row in tag_rows:
        result["tags"].append({**base(row), "name": row[4], "value": row[5]})
    for row in mask_rows:
        result["masks"].append({**base(row), "name": row[4]})
    for row in grant_rows:
        result["grants"].append({**base(row), "principal": row[4], "privilege": row[5], "scope": "COLUMN"})
    by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for column in result["columns"]:
        by_table[".".join(_fqn(column).split(".")[:3])].append(column)
    for row in table_grant_rows:
        for column in by_table[".".join(str(value) for value in row[:3])]:
            result["grants"].append({**column, "principal": row[3], "privilege": row[4], "scope": "TABLE"})
    for row in schema_grant_rows:
        prefix = ".".join(str(value) for value in row[:2]) + "."
        for table, table_columns in by_table.items():
            if table.startswith(prefix):
                for column in table_columns:
                    result["grants"].append({**column, "principal": row[2], "privilege": row[3], "scope": "SCHEMA"})
    for row in catalog_grant_rows:
        prefix = str(row[0]) + "."
        for table, table_columns in by_table.items():
            if table.startswith(prefix):
                for column in table_columns:
                    result["grants"].append({**column, "principal": row[1], "privilege": row[2], "scope": "CATALOG"})
    for row in policy_rows:
        for column in by_table[".".join(str(value) for value in row[:3])]:
            result["policies"].append({**column, "name": row[3]})
    # Deterministic governance masks through catalog-level ABAC policies, which
    # neither information_schema view above lists. Read the effective set per
    # table (include_inherited: catalog, schema and table level) through the
    # same env-only client; the SDK follows every page of the listing.
    column_tags: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for row in tag_rows:
        column_tags[_fqn(base(row))].add((str(row[4]), str(row[5])))
    from scripts.staged_policy_verify import normalize_policy

    result["abac_policies"] = []
    listed: set[tuple[str, str]] = set()
    for table, table_columns in sorted(by_table.items()):
        try:
            policies = list(client.policies.list_policies("TABLE", table, include_inherited=True))
        except Exception as exc:
            raise EvidenceUnavailable(
                f"could not list the ABAC policies effective on {table} ({type(exc).__name__}: {exc}); "
                "the report would wrongly show its columns unprotected") from exc
        result["abac_policies"].extend(abac_policy_rows(table, policies, table_columns, column_tags))
        listed.update((live["on"] or table, live["name"]) for live in map(normalize_policy, policies))
    result["abac_policy_listing"] = {"status": "complete", "tables": len(by_table), "policies": len(listed)}
    try:
        class_rows = query("SELECT catalog_name, schema_name, table_name, column_name, class_tag, latest_detected_time FROM system.data_classification.results WHERE concat(catalog_name, '.', schema_name, '.', table_name, '.', column_name) IN (" + keys + ")")
        classification_available = True
    except Exception:
        class_rows = []
        classification_available = False
    detected = set()
    for row in class_rows:
        column = base(row)
        detected.add(_fqn(column))
        result["classifications"].append({**column, "status": "detected", "scanned_at": row[5]})
        result["tags"].append({**column, "name": "data_classification", "value": row[4]})
    for column in result["columns"]:
        if _fqn(column) not in detected:
            status = "no_detection_record" if classification_available else "unavailable"
            result["classifications"].append({**column, "status": status, "scanned_at": None})
    return result


def write_report(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = "compliance-evidence-v" + report["schema_version"]
    json_path, md_path = output_dir / f"{stem}.json", output_dir / f"{stem}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def main(argv: list[str] | None = None, *, collector: Callable[..., dict[str, Any]] = collect_live_state) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-dir", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--warehouse-id", default=os.getenv("DATABRICKS_WAREHOUSE_ID"))
    parser.add_argument("--approved-by", default=os.getenv("GENIERAILS_EVIDENCE_APPROVED_BY", "unapproved"))
    parser.add_argument("--approved-at", default=os.getenv("GENIERAILS_EVIDENCE_APPROVED_AT"))
    args = parser.parse_args(argv)
    columns, tags = configured_columns(args.env_dir), configured_tags(args.env_dir)
    tables = configured_tables(args.env_dir)
    live = os.getenv(INTEGRATION_ENV, "").lower() in {"1", "true", "yes"}
    if live:
        if not args.warehouse_id:
            parser.error(f"--warehouse-id or DATABRICKS_WAREHOUSE_ID is required when {INTEGRATION_ENV}=1")
        try:
            state = collector(columns, tables, args.warehouse_id, args.env_dir / "auth.auto.tfvars")
        except EvidenceUnavailable as exc:
            print(f"ERROR: {exc}. No evidence was written.", file=sys.stderr)
            return 2
        report = assemble_report(state["columns"], classifications=state["classifications"], tags=state["tags"], masks=state["masks"], policies=state["policies"], abac_policies=state.get("abac_policies", []), grants=state["grants"], approved_by=args.approved_by, approved_at=args.approved_at, source="databricks-live")
        report["column_privileges_availability"] = state.get("column_privileges_availability", {})
        if "abac_policy_listing" in state:
            report["abac_policy_listing"] = state["abac_policy_listing"]
    else:
        report = assemble_report(columns, tags=tags, approved_by=args.approved_by, approved_at=args.approved_at)
    paths = write_report(report, args.output_dir or args.env_dir / "generated" / "evidence")
    print("Wrote " + " and ".join(str(path) for path in paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
