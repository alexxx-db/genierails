#!/usr/bin/env python3
"""The imports.jsonl that release-plan records and release MANIFEST= runs (import_existing.sh --record).

An import-only manifest skips the agent preflight, tag sync and staged
verification, so its record may hold only what the recorder writes: Terraform
imports of the resource shapes import_existing.sh produces, and UNSETs of stale
non-treatment tags on the env's own SQL warehouse. Every entry is checked when
the manifest is written and again before anything runs. A cleanup's stored
``statement`` is never executed: release rebuilds it from the checked
identifiers (``unset_sql``) and refuses one that doesn't match.

  import_record.py check FILE --env-dir DIR            # exit 3 with an ERROR line when refused
  import_record.py imports FILE --env-dir DIR --layer L  # address<TAB>id per checked import of L
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SHARED_ROOT = Path(__file__).resolve().parent.parent
if str(SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(SHARED_ROOT))

from scripts.resolve_env_config import WAREHOUSE_ID_RE, resolve_warehouse

TREATMENT_TAG = "gr_treatment"
IMPORT_FIELDS = {"kind", "layer", "address", "id"}
CLEANUP_FIELDS = {"kind", "layer", "entity_type", "entity_name", "tag_key", "statement", "warehouse_id"}
# Catalog, schema, table and column names a recorded UNSET may name (each backquoted).
IDENT_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,254}")
TAG_KEY_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:/-]{0,254}")
# Free text inside an import key or id (group, policy and tag value names): no quote, backslash or control.
TEXT_RE = re.compile(r'[^"\\\x00-\x1f\x7f|]{1,255}')
ENTITY_PARTS = {"catalogs": 1, "schemas": 2, "tables": 3, "columns": 4}
CLEANUP_ENTITY_PARTS = {"tables": 3, "columns": 4}
SECURABLES = ("CATALOG", "SCHEMA", "TABLE")
ADDRESS_RE = re.compile(r'module\.(\w+)\.(\w+)\.(\w+)(?:\["([^"\\]*)"\]|\[(0)\])')


class Invalid(ValueError):
    pass


def _ident(value: object, what: str) -> str:
    if not isinstance(value, str) or not IDENT_RE.fullmatch(value):
        raise Invalid(f"{what} {value!r} is not a plain identifier")
    return value


def _text(value: str, what: str) -> str:
    if not TEXT_RE.fullmatch(value):
        raise Invalid(f"{what} {value!r} has a character the recorder never writes")
    return value


def _tag_key(value: object) -> str:
    if not isinstance(value, str) or not TAG_KEY_RE.fullmatch(value):
        raise Invalid(f"tag key {value!r} is not a plain tag key")
    if value.casefold() == TREATMENT_TAG:  # Unity Catalog tag keys are case-insensitive
        raise Invalid(f"{TREATMENT_TAG} tags belong to the staged policy controller; a recorded entry never touches them")
    return value


def _entity(entity_type: object, entity_name: object, parts_by_type: dict[str, int]) -> list[str]:
    if entity_type not in parts_by_type:
        raise Invalid(f"entity type {entity_type!r} is not one of {', '.join(parts_by_type)}")
    parts = entity_name.split(".") if isinstance(entity_name, str) else []
    if len(parts) != parts_by_type[entity_type]:
        raise Invalid(f"{entity_type} name {entity_name!r} needs {parts_by_type[entity_type]} dot-separated parts")
    return [_ident(p, "name part") for p in parts]


def unset_sql(entity_type: str, entity_name: str, tag_key: str) -> str:
    """The only SQL a recorded cleanup runs, rebuilt from checked identifiers."""
    parts = _entity(entity_type, entity_name, CLEANUP_ENTITY_PARTS)
    tag_key = _tag_key(tag_key)
    table = ".".join(f"`{p}`" for p in parts[:3])
    column = f" ALTER COLUMN `{parts[3]}`" if entity_type == "columns" else ""
    return f"ALTER TABLE {table}{column} UNSET TAGS ('{tag_key}')"


def run_cleanups(cleanups: list[dict], warehouse_id: str, statements) -> int:
    """Run checked cleanups through ``statements`` (an SDK statement_execution); returns how many failed.

    Each statement is rebuilt here: the entry's stored ``statement`` is never read.
    """
    failed = 0
    for entry in cleanups:
        resp = statements.execute_statement(
            statement=unset_sql(entry["entity_type"], entry["entity_name"], entry["tag_key"]),
            warehouse_id=warehouse_id, wait_timeout="30s")
        raw_state = getattr(getattr(resp, "status", None), "state", None)
        state = raw_state.value if hasattr(raw_state, "value") else str(raw_state or "")
        if "SUCCEEDED" in state:
            print(f"  Cleared tag {entry['tag_key']} on {entry['entity_name']}")
        else:
            failed += 1
            print(f"  ✗ Could not clear {entry['tag_key']} on {entry['entity_name']} ({state})", file=sys.stderr)
    return failed


def cleanup_entry(entity_type: str, entity_name: str, tag_key: str, warehouse_id: str) -> dict:
    """What the recorder writes for one stale tag."""
    return {"kind": "cleanup", "layer": "data_access", "entity_type": entity_type, "entity_name": entity_name,
            "tag_key": tag_key, "statement": unset_sql(entity_type, entity_name, tag_key),
            "warehouse_id": warehouse_id}


def _expected_import_id(layer: str, rtype: str, name: str, key: str | None) -> str | re.Pattern:
    """The import id the recorder writes for this address (or the pattern a warehouse id matches)."""
    shape = (layer, rtype, name)
    if key is None:
        if shape in (("data_access", "databricks_sql_endpoint", "warehouse"),
                     ("workspace", "databricks_sql_endpoint", "warehouse")):
            return WAREHOUSE_ID_RE
        raise Invalid(f"module.{layer}.{rtype}.{name}[0] is not an address the recorder imports")
    if layer != "data_access":
        raise Invalid(f"the recorder imports only the warehouse into the {layer} layer")
    if shape == ("data_access", "databricks_data_classification_catalog_config", "classification"):
        return f"catalogs/{_ident(key, 'catalog')}/config"
    if shape == ("data_access", "databricks_entity_tag_assignment", "assignments"):
        fields = key.split("|", 3)
        if len(fields) != 4:
            raise Invalid(f"tag assignment key {key!r} is not entity_type|entity_name|tag_key|tag_value")
        entity_type, entity_name, tag_key, tag_value = fields
        _entity(entity_type, entity_name, ENTITY_PARTS)
        _tag_key(tag_key)
        _text(tag_value, "tag value")
        return f"{entity_type},{entity_name},{tag_key}"
    if shape == ("data_access", "databricks_grant", "catalog_access"):
        catalog, sep, group = key.partition("|")
        if not sep:
            raise Invalid(f"catalog grant key {key!r} is not catalog|group")
        return f"catalog/{_ident(catalog, 'catalog')}/{_text(group, 'group')}"
    if shape == ("data_access", "databricks_grant", "terraform_sp_manage_catalog"):
        return re.compile(re.escape(f"catalog/{_ident(key, 'catalog')}/") + r'[^"\\\x00-\x1f\x7f|/]{1,255}')
    if shape == ("data_access", "databricks_policy_info", "policies"):
        name_ = _text(key, "policy name")
        if "," in name_:
            raise Invalid(f"policy name {name_!r} has a comma")
        return re.compile(r"(?:%s),([A-Za-z0-9_][A-Za-z0-9_-]{0,254}),\1_%s"
                          % ("|".join(SECURABLES), re.escape(name_)))
    raise Invalid(f"module.{layer}.{rtype}.{name} is not a resource the recorder imports")


def _check_import(entry: dict) -> None:
    address, import_id, layer = entry["address"], entry["id"], entry["layer"]
    if not isinstance(address, str) or not isinstance(import_id, str):
        raise Invalid("address and id must be strings")
    match = ADDRESS_RE.fullmatch(address)
    if not match:
        raise Invalid(f"address {address!r} is not an address the recorder writes")
    module, rtype, name, key, _index = match.groups()
    if module != layer:
        raise Invalid(f"address {address!r} is not in the {layer} layer")
    expected = _expected_import_id(layer, rtype, name, key)
    ok = expected.fullmatch(import_id) if isinstance(expected, re.Pattern) else import_id == expected
    if not ok:
        raise Invalid(f"id {import_id!r} is not the id the recorder writes for {address}")


def _check_cleanup(entry: dict, warehouse: str) -> None:
    if entry["layer"] != "data_access":
        raise Invalid("stale-tag cleanups are recorded only in the data_access layer")
    statement = unset_sql(entry["entity_type"], entry["entity_name"], entry["tag_key"])
    if entry["statement"] != statement:
        raise Invalid(f"statement {entry['statement']!r} is not the UNSET the recorder writes ({statement})")
    if not warehouse:
        raise Invalid("this env configures no SQL warehouse (sql_warehouse_id) to run the cleanup on")
    if entry["warehouse_id"] != warehouse:
        raise Invalid(f"warehouse_id {entry['warehouse_id']!r} is not this env's SQL warehouse ({warehouse})")


def configured_warehouse(env_dir: Path) -> str:
    """The env's SQL warehouse: sql_warehouse_id, else the one the workspace layer created; "" when none."""
    try:
        return resolve_warehouse(Path(env_dir).resolve(), allow_unset=True)
    except (ValueError, OSError) as exc:
        raise Invalid(f"the env's SQL warehouse can't be resolved: {exc}") from exc


def validate(entries: list, env_dir: Path) -> list[dict]:
    """Every entry, checked; raises Invalid naming the first one the recorder could not have written."""
    warehouse = None
    for number, entry in enumerate(entries, 1):
        try:
            if not isinstance(entry, dict):
                raise Invalid("not a JSON object")
            fields = {"import": IMPORT_FIELDS, "cleanup": CLEANUP_FIELDS}.get(entry.get("kind"))
            if fields is None:
                raise Invalid(f"kind {entry.get('kind')!r} is not import or cleanup")
            if set(entry) != fields:
                extra, missing = sorted(set(entry) - fields), sorted(fields - set(entry))
                raise Invalid(f"fields differ from a recorded {entry['kind']}"
                              + (f"; unexpected {', '.join(extra)}" if extra else "")
                              + (f"; missing {', '.join(missing)}" if missing else ""))
            if entry["layer"] not in ("data_access", "workspace"):
                raise Invalid(f"layer {entry['layer']!r} is not data_access or workspace")
            if entry["kind"] == "import":
                _check_import(entry)
            else:
                if warehouse is None:
                    warehouse = configured_warehouse(env_dir)
                _check_cleanup(entry, warehouse)
        except Invalid as exc:
            raise Invalid(f"entry {number}: {exc}") from None
    return entries


def load(path: Path, env_dir: Path) -> list[dict]:
    try:
        entries = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        raise Invalid(f"not readable: {exc}") from exc
    return validate(entries, env_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check", "imports"))
    parser.add_argument("record", type=Path)
    parser.add_argument("--env-dir", type=Path, required=True)
    parser.add_argument("--layer", default="")
    args = parser.parse_args(argv)
    try:
        entries = load(args.record, args.env_dir)
    except Invalid as exc:
        print(f"ERROR: the recorded imports {args.record} hold an entry release-plan never records ({exc}). "
              "Nothing was imported or cleared; run make release-plan again.", file=sys.stderr)
        return 3
    if args.command == "imports":
        for entry in entries:
            if entry["kind"] == "import" and entry["layer"] == args.layer:
                print(entry["address"] + "\t" + entry["id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
