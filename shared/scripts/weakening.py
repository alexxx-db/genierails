#!/usr/bin/env python3
"""Refuse deterministic-governance changes that weaken effective protection."""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Mapping

from ack_file import AckFileError, load_consequences, merge_consequences, parse_file, reconcile


@dataclass(frozen=True)
class Protection:
    level: str
    consequence: str
    # The gr_treatment value whose policies give this level ("" when none or unknown).
    value: str = ""


@dataclass(frozen=True)
class RowFilter:
    values: frozenset[str] | None

    @property
    def consequence(self) -> str:
        return "all rows" if self.values is None else "rows where value is in {" + ", ".join(sorted(self.values)) + "}"


@dataclass
class GovernanceState:
    columns: dict[str, dict[str, Protection]] = field(default_factory=dict)
    raw_exempt_principals: set[str] = field(default_factory=set)
    partial_versions: dict[str, str] = field(default_factory=dict)
    row_filters: dict[str, dict[str, RowFilter]] = field(default_factory=dict)
    table_readers: dict[str, set[str]] = field(default_factory=dict)


@dataclass(frozen=True, order=True)
class Weakening:
    ack_key: str
    category: str
    object_name: str
    principal: str
    before: str
    after: str

    @property
    def consequence(self) -> str:
        return f"{self.before} -> {self.after}"


@dataclass(frozen=True)
class CheckResult:
    all_weakenings: tuple[Weakening, ...]
    acknowledged: tuple[Weakening, ...]
    unacknowledged: tuple[Weakening, ...]

    @property
    def ok(self) -> bool:
        return not self.unacknowledged

    @property
    def consequences(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for item in self.all_weakenings:
            prior = result.get(item.ack_key)
            result[item.ack_key] = item.consequence if prior is None else f"{prior}; {item.consequence}"
        return result


_STRONGEST = 2
# An adopted custom mask (design 18.4.1); the same pattern as governance_policies.CUSTOM_TREATMENT.
_CUSTOM_VALUE = re.compile(r"custom_[a-z0-9]+(?:_[a-z0-9]+)*__[a-z0-9_]+__[0-9a-f]{8}")


def protection_rank(level: str) -> int:
    normalized = level.strip().lower()
    if normalized == "raw":
        return 0
    if normalized == "partial" or normalized.startswith("partial:"):
        return 1
    if normalized in {"redacted", "null"}:
        return 2
    raise ValueError(f"unknown protection level {level!r}; expected raw, partial[:version], redacted, or NULL")


def _identifier(value: str, parts: set[int], label: str) -> str:
    normalized = value.strip().lower()
    if len(normalized.split(".")) not in parts or any(not part for part in normalized.split(".")):
        raise ValueError(f"{label} has an invalid Unity Catalog identifier: {value!r}")
    return normalized


def _normalized(state: GovernanceState) -> GovernanceState:
    columns: dict[str, dict[str, Protection]] = {}
    for raw_column, principals in state.columns.items():
        column = _identifier(raw_column, {4}, "column")
        if column in columns and columns[column] != principals:
            raise ValueError(f"case-colliding column state for {raw_column!r}")
        columns[column] = dict(principals)
    tables: dict[str, set[str]] = {}
    for raw_table, principals in state.table_readers.items():
        table = _identifier(raw_table, {3}, "table")
        tables.setdefault(table, set()).update(principals)
    filters: dict[str, dict[str, RowFilter]] = {}
    for raw_table, principals in state.row_filters.items():
        table = _identifier(raw_table, {3}, "row-filter table")
        if table in filters and filters[table] != principals:
            raise ValueError(f"case-colliding row-filter state for {raw_table!r}")
        filters[table] = dict(principals)
    partial: dict[str, str] = {}
    for raw_object, value in state.partial_versions.items():
        obj = _identifier(raw_object, {1, 4}, "partial-version object")
        if obj in partial and partial[obj].strip().lower() != value.strip().lower():
            raise ValueError(f"case-colliding partial-version state for {raw_object!r}")
        partial[obj] = value
    return GovernanceState(
        columns, set(state.raw_exempt_principals), partial, filters, tables
    )


def _governed_tables(state: GovernanceState) -> set[str]:
    tables = set(state.table_readers) | set(state.row_filters)
    tables.update(column.rsplit(".", 1)[0] for column in state.columns)
    return tables


def _issue(env: str, category: str, obj: str, principal: str, before: str, after: str) -> Weakening:
    return Weakening(f"{env}:weaken:{obj}:{principal}", category, obj, principal, before, after)


def find_weakenings(before: GovernanceState, after: GovernanceState, env: str) -> tuple[Weakening, ...]:
    """Compare unioned, case-normalized state; missing data fails closed."""
    before, after = _normalized(before), _normalized(after)
    found: dict[tuple[str, str, str], Weakening] = {}

    def add(item: Weakening) -> None:
        found[(item.ack_key, item.category, item.consequence)] = item

    for column in set(before.columns) | set(after.columns):
        prior_principals = before.columns.get(column)
        current_principals = after.columns.get(column)
        for principal in set(prior_principals or {}) | set(current_principals or {}):
            prior = (prior_principals or {}).get(principal)
            current = (current_principals or {}).get(principal)
            if prior is None:
                # Unknown before-state is treated as weaker-than-after unless
                # the after level is already the strongest (redacted/NULL).
                if current is None or protection_rank(current.level) < _STRONGEST:
                    add(_issue(env, "missing_before", column, principal, "missing before state", current.consequence if current else "missing after state"))
            elif current is None:
                add(_issue(env, "missing_after", column, principal, prior.consequence, "missing after state (protection unknown)"))
            elif protection_rank(current.level) < protection_rank(prior.level):
                add(_issue(env, "protection", column, principal, prior.consequence, current.consequence))
            elif (protection_rank(prior.level) == protection_rank(current.level) == 1
                  and _CUSTOM_VALUE.fullmatch(current.value) and current.value != prior.value):
                # partial -> partial keeps the rank, but custom SQL decides what is
                # revealed: a library mask swapped for a custom one, one custom mask
                # for another, or a custom mask's SQL edited (a new generation).
                add(_issue(env, "custom_mask_changed", column, principal, prior.consequence, current.consequence))

    # An exemption applies to every mask the principal meets, and it can reach a
    # governed table through groups or grants GenieRails doesn't see here, so a
    # new exemption is one issue per governed table, listed columns or not.
    governed = _governed_tables(before) | _governed_tables(after)
    for principal in after.raw_exempt_principals - before.raw_exempt_principals:
        for table in governed:
            direct = principal in after.table_readers.get(table, set())
            add(_issue(
                env, "raw_exempt_principal", table, principal,
                "subject to masks" if direct else "subject to masks wherever it can read",
                "sees raw through raw_exempt_principals"
                + ("" if direct else " (if it can read via any group or grant)"),
            ))

    for obj in set(before.partial_versions) | set(after.partial_versions):
        old = before.partial_versions.get(obj)
        new = after.partial_versions.get(obj)
        if new is not None and new.strip().lower() == "raw" and (
            old is None or old.strip().lower() != "raw"
        ):
            add(_issue(
                env, "partial_raw", obj, "partial",
                old or "missing before partial version", "raw through partial='raw'",
            ))
        elif old is not None and new is None:
            add(_issue(
                env, "missing_partial_after", obj, "partial", old,
                "missing after partial version (protection unknown)",
            ))

    for table in set(before.row_filters) | set(after.row_filters):
        old_groups = before.row_filters.get(table, {})
        new_groups = after.row_filters.get(table, {})
        if table in before.row_filters and table not in after.row_filters:
            # Readers outside every filter group saw zero rows; without the
            # filter they see all rows.
            add(_issue(
                env, "row_filter_removed", table, "*",
                "no rows for readers outside the filter", "all rows (filter removed)",
            ))
        for principal in set(old_groups) | set(new_groups):
            old = old_groups.get(principal)
            new = new_groups.get(principal)
            if old is None:
                # The first filter tightens an unfiltered table. A group added
                # to an already-existing filter moves from zero rows to some
                # or all rows and is therefore widening.
                if table in before.row_filters and new is not None and new.values != frozenset():
                    add(_issue(
                        env, "row_filter_new_group", table, principal,
                        "no rows (group not in filter)", new.consequence if new else "missing after filter",
                    ))
                continue
            if new is None:
                add(_issue(env, "row_filter_removed", table, principal, old.consequence, "all rows (filter removed)"))
                continue
            # None means unrestricted. None -> None is unchanged; a finite set
            # -> None, or exposure of any new value, is widening.
            if old.values is not None and (
                new.values is None or not new.values.issubset(old.values)
            ):
                add(_issue(env, "row_filter_widened", table, principal, old.consequence, new.consequence))

    for table in set(before.table_readers) | set(after.table_readers):
        old_readers = before.table_readers.get(table, set())
        new_readers = after.table_readers.get(table, set())
        for principal in new_readers - old_readers:
            add(Weakening(
                f"{env}:reader:{table}:{principal}", "table_reader", table, principal,
                "no GenieRails SELECT", "GenieRails grants SELECT",
            ))
        # Missing table-reader data is unknown, not evidence that access was revoked.
        if table in before.table_readers and table not in after.table_readers:
            for principal in old_readers:
                add(_issue(
                    env, "missing_table_readers_after", table, principal,
                    "known table reader", "missing after table-reader state",
                ))
    return tuple(sorted(found.values()))


def check_weakenings(
    before: GovernanceState, after: GovernanceState, env: str, acknowledgement_keys: set[str]
) -> CheckResult:
    found = find_weakenings(before, after, env)
    acknowledged = tuple(item for item in found if item.ack_key in acknowledgement_keys)
    unacknowledged = tuple(item for item in found if item.ack_key not in acknowledgement_keys)
    return CheckResult(found, acknowledged, unacknowledged)


def _state(payload: Mapping[str, object]) -> GovernanceState:
    columns = {
        str(column): {
            str(principal): Protection(str(value["level"]), str(value["consequence"]), str(value.get("value") or ""))
            for principal, value in dict(principals).items()
        }
        for column, principals in dict(payload.get("columns", {})).items()
    }
    filters = {
        str(table): {
            str(principal): RowFilter(None if values is None else frozenset(map(str, values)))
            for principal, values in dict(principals).items()
        }
        for table, principals in dict(payload.get("row_filters", {})).items()
    }
    return GovernanceState(
        columns=columns,
        raw_exempt_principals=set(map(str, payload.get("raw_exempt_principals", []))),
        partial_versions={str(k): str(v) for k, v in dict(payload.get("partial_versions", {})).items()},
        row_filters=filters,
        table_readers={str(k): set(map(str, v)) for k, v in dict(payload.get("table_readers", {})).items()},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True)
    parser.add_argument("--before", type=Path, required=True, help="before-state JSON")
    parser.add_argument("--after", type=Path, required=True, help="after-state JSON")
    parser.add_argument("--ack-file", type=Path, required=True)
    parser.add_argument(
        "--all-consequences", type=Path, action="append", default=[],
        help="JSON consequences from reader/unclassified checks (repeatable); "
        "stale ACKs are reconciled once across all of them",
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)
    try:
        entries = parse_file(args.ack_file, args.env)
        result = check_weakenings(
            _state(json.loads(args.before.read_text())),
            _state(json.loads(args.after.read_text())), args.env,
            {entry.key for entry in entries},
        )
        all_consequences = merge_consequences(
            result.consequences,
            *(load_consequences(path) for path in args.all_consequences),
        )
        _, unmatched = reconcile(entries, all_consequences)
        ok = result.ok and not unmatched
        if args.json:
            print(json.dumps({
                "ok": ok,
                "acknowledged": [asdict(item) for item in result.acknowledged],
                "unacknowledged": [asdict(item) for item in result.unacknowledged],
                "unmatched_acknowledgements": [entry.key for entry in unmatched],
                "consequences": all_consequences,
            }, indent=2, sort_keys=True))
        else:
            for item in result.acknowledged:
                print(f"ACKNOWLEDGED {item.ack_key} [{item.category}]: {item.consequence}")
            for item in result.unacknowledged:
                print(f"ERROR: unacknowledged {item.ack_key} [{item.category}]: {item.consequence}", file=sys.stderr)
            for entry in unmatched:
                print(f"ERROR: line {entry.line}: acknowledgement matches nothing: {entry.key}", file=sys.stderr)
            if ok:
                print("OK: no unacknowledged weakening and no stale acknowledgement")
        return 0 if ok else 1
    except (AckFileError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"ERROR: weakening check failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
