#!/usr/bin/env python3
"""Inventory effective readers and grant-capable principals for a governed table.

The inventory is bounded by what the deployer can see in Unity Catalog. It
fails rather than under-reports: a missing or invisible table, an empty owner,
a failed or timed-out query, or a deployer without authority to see every
grant on each securable (owner, MANAGE, or metastore admin) stops the run.
Delta Sharing recipients and file paths (READ FILES on external locations or
volumes) are not covered.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Protocol, Sequence


READ_PRIVILEGES = frozenset({"SELECT", "ALL PRIVILEGES"})
GRANT_PRIVILEGES = frozenset({"MANAGE"})


@dataclass(frozen=True, order=True)
class Grant:
    securable_type: str
    securable_name: str
    principal: str
    privilege: str

    def normalized(self) -> "Grant":
        return Grant(
            self.securable_type.upper(), self.securable_name.lower(),
            self.principal, self.privilege.upper().replace("_", " "),
        )


class InventorySource(Protocol):
    def grants(self, securable_type: str, securable_name: str) -> Iterable[Grant]: ...
    def owner(self, securable_type: str, securable_name: str) -> str | None: ...
    def dependent_views(self, object_name: str) -> Iterable[str]: ...
    def group_members(self) -> Mapping[str, Iterable[str]]: ...
    def metastore_admins(self) -> Iterable[str]: ...
    def current_principal(self) -> str | None: ...


@dataclass(frozen=True)
class PrincipalEvidence:
    principal: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Inventory:
    table: str
    dependent_views: tuple[str, ...]
    readers: tuple[PrincipalEvidence, ...]
    granters: tuple[PrincipalEvidence, ...]
    group_closure: dict[str, tuple[str, ...]]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class FakeSource:
    """Small in-memory source used by unit tests and downstream callers."""
    grant_rows: list[Grant] = field(default_factory=list)
    owners: dict[tuple[str, str], str] = field(default_factory=dict)
    dependencies: dict[str, set[str]] = field(default_factory=dict)
    groups: dict[str, set[str]] = field(default_factory=dict)
    admins: set[str] = field(default_factory=set)
    deployer: str | None = None

    def grants(self, securable_type: str, securable_name: str) -> Iterable[Grant]:
        wanted = (securable_type.upper(), securable_name.lower())
        return [g for g in self.grant_rows if (g.securable_type.upper(), g.securable_name.lower()) == wanted]

    def owner(self, securable_type: str, securable_name: str) -> str | None:
        return self.owners.get((securable_type.upper(), securable_name.lower()))

    def dependent_views(self, object_name: str) -> Iterable[str]:
        return self.dependencies.get(object_name.lower(), set())

    def group_members(self) -> Mapping[str, Iterable[str]]:
        return self.groups

    def metastore_admins(self) -> Iterable[str]:
        return self.admins

    def current_principal(self) -> str | None:
        return self.deployer


@dataclass(frozen=True)
class ReaderFinding:
    ack_key: str
    principal: str
    consequence: str


@dataclass(frozen=True)
class ReaderCheck:
    authorized: tuple[str, ...]
    acknowledged: tuple[ReaderFinding, ...]
    unacknowledged: tuple[ReaderFinding, ...]

    @property
    def ok(self) -> bool:
        return not self.unacknowledged

    @property
    def consequences(self) -> dict[str, str]:
        return {item.ack_key: item.consequence for item in (*self.acknowledged, *self.unacknowledged)}


def _parents(table: str) -> tuple[tuple[str, str], ...]:
    parts = table.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise ValueError(f"table must be catalog.schema.table, got {table!r}")
    catalog, schema, _ = parts
    return (("CATALOG", catalog), ("SCHEMA", f"{catalog}.{schema}"), ("TABLE", table))


def _parents_of(scope: tuple[str, str]) -> tuple[tuple[str, str], ...]:
    """The securable and each parent: TABLE -> SCHEMA -> CATALOG."""
    securable_type, name = scope
    parts = name.split(".")
    chain = [("CATALOG", parts[0])]
    if securable_type in {"SCHEMA", "TABLE"}:
        chain.append(("SCHEMA", ".".join(parts[:2])))
    if securable_type == "TABLE":
        chain.append(("TABLE", name))
    return tuple(chain)


def _expand(principal: str, groups: Mapping[str, Iterable[str]]) -> set[str]:
    """Include a granted principal and every transitively nested member."""
    expanded: set[str] = set()
    visiting: set[str] = set()

    def visit(item: str) -> None:
        if item in expanded:
            return
        expanded.add(item)
        if item in visiting:
            return
        visiting.add(item)
        for member in groups.get(item, ()):  # cycles are tolerated
            visit(str(member))
        visiting.remove(item)

    visit(principal)
    return expanded


def inventory_readers(
    source: InventorySource,
    table: str,
    genierails_grants: Iterable[Grant] = (),
) -> Inventory:
    """Compute readers not granted by GenieRails and report granters separately."""
    normalized_table = table.lower()
    scopes = list(_parents(normalized_table))
    # Owner discovery is also the visibility/existence proof. Never turn a
    # missing or invisible base table into an empty, apparently safe report.
    base_owner = source.owner("TABLE", normalized_table)
    if not base_owner:
        raise RuntimeError(
            f"governed table {normalized_table} does not exist, is not visible, or has no visible owner"
        )
    views: set[str] = set()
    queue = [table.lower()]
    while queue:
        base = queue.pop(0)
        for view in source.dependent_views(base):
            normalized = str(view).lower()
            if normalized not in views and normalized != table.lower():
                views.add(normalized)
                queue.append(normalized)
    for view in sorted(views):
        scopes.extend(_parents(view))

    managed = {grant.normalized() for grant in genierails_grants}
    groups = source.group_members()
    deployer = source.current_principal()
    if not deployer:
        raise RuntimeError("cannot determine the deployer principal")
    deployer_identities = {deployer} | {
        str(group) for group in groups if deployer in _expand(str(group), groups)
    }
    admins = [str(admin) for admin in source.metastore_admins()]
    if not admins:
        raise RuntimeError("cannot determine the metastore admin")
    reader_reasons: dict[str, set[str]] = {}
    granter_reasons: dict[str, set[str]] = {}

    def add(target: dict[str, set[str]], principal: str, reason: str) -> None:
        for effective in _expand(principal, groups):
            target.setdefault(effective, set()).add(reason)

    authority: dict[tuple[str, str], set[str]] = {}
    seen_scopes: set[tuple[str, str]] = set()
    for securable_type, name in scopes:
        scope = (securable_type, name.lower())
        if scope in seen_scopes:
            continue
        seen_scopes.add(scope)
        for raw_grant in source.grants(*scope):
            grant = raw_grant.normalized()
            if grant.privilege in READ_PRIVILEGES and grant not in managed:
                add(reader_reasons, grant.principal, f"{grant.privilege} on {scope[0]} {scope[1]}")
            if grant.privilege in GRANT_PRIVILEGES:
                add(granter_reasons, grant.principal, f"MANAGE on {scope[0]} {scope[1]}")
                authority.setdefault(scope, set()).add(grant.principal)

        owner = base_owner if scope == ("TABLE", normalized_table) else source.owner(*scope)
        if not owner:
            raise RuntimeError(
                f"cannot determine owner of {scope[0]} {scope[1]}; object may be invisible"
            )
        authority.setdefault(scope, set()).add(owner)
        owner_reason = f"owner of {scope[0]} {scope[1]}"
        add(granter_reasons, owner, owner_reason)
        # Owners of a dependent view and its schema/catalog can expose the
        # governed base data and are readers as well as grant-capable risks.
        add(reader_reasons, owner, owner_reason)

    for admin in admins:
        add(reader_reasons, admin, "metastore admin")
        add(granter_reasons, admin, "metastore admin")

    # information_schema only returns the grants the caller may see. Only an
    # owner or MANAGE holder of the securable or a parent, or the metastore
    # admin, sees all of them; anyone else would get a silently partial list.
    is_admin = any(deployer_identities & _expand(admin, groups) for admin in admins)
    if not is_admin:
        for scope in sorted(seen_scopes):
            holders: set[str] = set()
            for parent in _parents_of(scope):
                for principal in authority.get(parent, ()):
                    holders |= _expand(principal, groups)
            if not deployer_identities & holders:
                raise RuntimeError(
                    f"deployer {deployer} cannot see every grant on {scope[0]} {scope[1]}: "
                    "it is not the metastore admin, nor an owner or MANAGE holder of the "
                    "object or a parent, so the reader inventory would be incomplete"
                )

    def evidence(values: Mapping[str, set[str]]) -> tuple[PrincipalEvidence, ...]:
        return tuple(
            PrincipalEvidence(principal, tuple(sorted(reasons)))
            for principal, reasons in sorted(values.items())
        )

    closure = {
        str(group): tuple(sorted(_expand(str(group), groups)))
        for group in sorted(groups)
    }
    return Inventory(
        normalized_table, tuple(sorted(views)), evidence(reader_reasons),
        evidence(granter_reasons), closure,
    )


def check_readers(
    inventory: Inventory,
    env: str,
    tiers: Iterable[str],
    raw_exempt_principals: Iterable[str],
    acknowledgement_keys: set[str],
) -> ReaderCheck:
    """Refuse every external reader not covered by a tier, exemption, or ACK."""
    tiers = set(map(str, tiers))
    exemptions = set(map(str, raw_exempt_principals))
    tier_members = set(tiers)
    for tier in tiers:
        tier_members.update(inventory.group_closure.get(tier, ()))
    exempt_members = set(exemptions)
    for exempt in exemptions:
        exempt_members.update(inventory.group_closure.get(exempt, ()))
    authorized: list[str] = []
    acknowledged: list[ReaderFinding] = []
    unacknowledged: list[ReaderFinding] = []
    for evidence in inventory.readers:
        principal = evidence.principal
        if principal in tier_members or principal in exempt_members:
            authorized.append(principal)
            continue
        key = f"{env}:reader:{inventory.table}:{principal}"
        finding = ReaderFinding(
            key, principal,
            f"{principal} can read {inventory.table} via " + "; ".join(evidence.reasons),
        )
        (acknowledged if key in acknowledgement_keys else unacknowledged).append(finding)
    return ReaderCheck(tuple(sorted(authorized)), tuple(acknowledged), tuple(unacknowledged))


class DatabricksSource:
    """Databricks-backed source. SDK imports occur only when instantiated."""

    def __init__(self, warehouse_id: str, client: object | None = None, timeout_seconds: int = 120,
                 auth_file: Path = Path("auth.auto.tfvars")):
        if client is None:
            from scripts.auth_configured import workspace_client
            client = workspace_client(auth_file)
        self.client = client
        self.warehouse_id = warehouse_id
        self.timeout_seconds = timeout_seconds
        self._groups: dict[str, set[str]] | None = None
        self._names: dict[tuple[str, str], str] = {}

    @staticmethod
    def _literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def _query(self, statement: str) -> list[list[str | None]]:
        execution = self.client.statement_execution
        result = execution.execute_statement(
            warehouse_id=self.warehouse_id, statement=statement, wait_timeout="50s"
        )
        deadline = time.monotonic() + self.timeout_seconds
        while str(getattr(result.status, "state", "")).split(".")[-1] not in {
            "SUCCEEDED", "FAILED", "CANCELED", "CLOSED"
        }:
            if time.monotonic() >= deadline:
                execution.cancel_execution(result.statement_id)
                raise TimeoutError("reader inventory SQL query timed out")
            time.sleep(1)
            result = execution.get_statement(result.statement_id)
        state = str(getattr(result.status, "state", "")).split(".")[-1]
        if state != "SUCCEEDED":
            raise RuntimeError(f"reader inventory SQL failed ({state}): {getattr(result.status, 'error', '')}")
        rows = list(getattr(result.result, "data_array", None) or [])
        chunks = int(getattr(getattr(result, "manifest", None), "total_chunk_count", 1) or 1)
        for index in range(1, chunks):
            chunk = execution.get_statement_result_chunk_n(result.statement_id, index)
            rows.extend(getattr(chunk, "data_array", None) or [])
        return rows

    def grants(self, securable_type: str, securable_name: str) -> Iterable[Grant]:
        catalog, *rest = securable_name.split(".")
        if securable_type == "CATALOG":
            view, predicate = "catalog_privileges", f"catalog_name = {self._literal(catalog)}"
        elif securable_type == "SCHEMA":
            view = "schema_privileges"
            predicate = f"catalog_name = {self._literal(catalog)} AND schema_name = {self._literal(rest[0])}"
        else:
            view = "table_privileges"
            predicate = (
                f"table_catalog = {self._literal(catalog)} AND table_schema = {self._literal(rest[0])} "
                f"AND table_name = {self._literal(rest[1])}"
            )
        rows = self._query(
            f"SELECT grantee, privilege_type FROM system.information_schema.{view} WHERE {predicate}"
        )
        return [Grant(securable_type, securable_name, str(row[0]), str(row[1])) for row in rows]

    def owner(self, securable_type: str, securable_name: str) -> str | None:
        parts = securable_name.split(".")
        if securable_type == "CATALOG":
            query = f"SELECT catalog_owner FROM system.information_schema.catalogs WHERE catalog_name={self._literal(parts[0])}"
        elif securable_type == "SCHEMA":
            query = (
                "SELECT schema_owner FROM system.information_schema.schemata WHERE "
                f"catalog_name={self._literal(parts[0])} AND schema_name={self._literal(parts[1])}"
            )
        else:
            query = (
                "SELECT table_owner FROM system.information_schema.tables WHERE "
                f"table_catalog={self._literal(parts[0])} AND table_schema={self._literal(parts[1])} "
                f"AND table_name={self._literal(parts[2])}"
            )
        rows = self._query(query)
        return str(rows[0][0]) if rows and rows[0][0] else None

    def dependent_views(self, object_name: str) -> Iterable[str]:
        catalog, schema, table = object_name.split(".")
        rows = self._query(
            "SELECT view_catalog, view_schema, view_name FROM system.information_schema.view_table_usage WHERE "
            f"table_catalog={self._literal(catalog)} AND table_schema={self._literal(schema)} "
            f"AND table_name={self._literal(table)}"
        )
        return [".".join(map(str, row[:3])) for row in rows]

    def _member_name(self, member: object) -> str:
        """Resolve a SCIM member to the name Unity Catalog grants use."""
        member_id = str(getattr(member, "value", "") or "")
        kind = str(getattr(member, "ref", "") or "").split("/")[0]
        if (kind, member_id) not in self._names:
            if kind == "Users":
                name = getattr(self.client.users.get(member_id), "user_name", None)
            elif kind == "ServicePrincipals":
                name = getattr(self.client.service_principals.get(member_id), "application_id", None)
            elif kind == "Groups":
                name = getattr(self.client.groups.get(member_id), "display_name", None)
            else:
                raise RuntimeError(f"group member {member_id!r} has unknown type {kind!r}")
            if not name:
                raise RuntimeError(f"cannot resolve {kind} member {member_id!r} to a user name/email, application id or group name")
            self._names[(kind, member_id)] = str(name)
        return self._names[(kind, member_id)]

    def group_members(self) -> Mapping[str, Iterable[str]]:
        if self._groups is None:
            groups: dict[str, set[str]] = {}
            for group in self.client.groups.list(attributes="id,displayName,members"):
                if not getattr(group, "display_name", None):
                    raise RuntimeError(f"group {getattr(group, 'id', '?')!r} has no visible name")
                groups[str(group.display_name)] = {
                    self._member_name(member) for member in group.members or []
                }
            self._groups = groups
        return self._groups

    def current_principal(self) -> str | None:
        me = self.client.current_user.me()
        return getattr(me, "user_name", None)

    def metastore_admins(self) -> Iterable[str]:
        current = self.client.metastores.current()
        metastore_id = getattr(current, "metastore_id", None)
        if not metastore_id:
            raise RuntimeError("cannot resolve the current metastore")
        metastore = self.client.metastores.get(metastore_id)
        owner = getattr(metastore, "owner", None)
        if not owner:
            raise RuntimeError(f"metastore {metastore_id} has no visible owner")
        return (str(owner),)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tables", nargs="+", metavar="table", help="catalog.schema.table (one or more)")
    parser.add_argument("--warehouse-id", required=True)
    parser.add_argument("--auth-file", type=Path, default=Path("auth.auto.tfvars"))
    parser.add_argument("--genierails-grants", type=Path, help="optional JSON list of Grant objects")
    parser.add_argument("--env", required=True)
    parser.add_argument("--tier", action="append", default=[])
    parser.add_argument("--raw-exempt-principal", action="append", default=[])
    parser.add_argument("--ack-file", type=Path, required=True)
    parser.add_argument(
        "--consequences-out", type=Path,
        help="write reader consequences JSON for the one-time ACK reconciliation",
    )
    args = parser.parse_args(argv)
    try:
        managed: Sequence[Grant] = ()
        if args.genierails_grants:
            managed = [Grant(**row) for row in json.loads(args.genierails_grants.read_text())]
        from ack_file import parse_file
        entries = parse_file(args.ack_file, args.env)
        source = DatabricksSource(args.warehouse_id, None, 120, args.auth_file)
        keys = {entry.key for entry in entries}
        reports, checks = [], []
        for table in args.tables:
            report = inventory_readers(source, table, managed)
            reports.append(report.to_dict())
            checks.append(check_readers(report, args.env, args.tier, args.raw_exempt_principal, keys))
        consequences: dict[str, str] = {}
        for check in checks:
            consequences.update(check.consequences)
        print(json.dumps({
            "inventories": reports,
            "reader_checks": [asdict(check) for check in checks],
            "consequences": consequences,
        }, indent=2, sort_keys=True))
        if args.consequences_out:
            # Stale-ACK reconciliation happens once, across every check, in
            # ack_file.py or weakening.py; an ACK for another table isn't stale here.
            args.consequences_out.write_text(json.dumps(consequences, indent=2, sort_keys=True))
        unacknowledged = sum(len(check.unacknowledged) for check in checks)
        if unacknowledged:
            for check in checks:
                for item in check.unacknowledged:
                    print(f"ERROR: unacknowledged {item.ack_key}: {item.consequence}", file=sys.stderr)
            print(
                f"ERROR: {unacknowledged} reader(s) are outside tiers, raw exemptions, and ACK",
                file=sys.stderr,
            )
            return 1
        return 0
    except Exception as exc:
        print(f"ERROR: reader inventory failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
