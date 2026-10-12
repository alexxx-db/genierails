#!/usr/bin/env python3
"""Parse and reconcile committed deterministic-governance acknowledgements.

Each ``envs/<env>/ack.txt`` line is ``<env>:<kind>:<object>[:<principal>]``, for
example ``prod:weaken:cat.sch.t.col:etl-sp``. An entry whose env differs from
the env being checked is refused. The file is an approval record, so the
repository must make it code-owned (a ``CODEOWNERS`` rule for
``envs/*/ack.txt`` naming the team that approves governance exceptions).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


_IDENT = r"[A-Za-z0-9_][A-Za-z0-9_$-]*"
_ENV = re.compile(rf"^{_IDENT}$")
_OBJECT = re.compile(rf"^{_IDENT}(?:\.{_IDENT})*$")
_PRINCIPAL = re.compile(r"^[^\s:#,]+(?: [^\s:#,]+)*$")
_COMMIT = re.compile(r"^[0-9a-fA-F]{7,64}$")
_OBJECT_PARTS = {
    "weaken": {1, 3, 4},  # treatment, table, or column
    "unclassified": {4},
    "reader": {3},
    "revoke": {1, 3},  # an agent (CAN_RUN) or a table (SELECT), migration only
}


class AckFileError(ValueError):
    """The acknowledgement file is malformed or cannot be reconciled."""


@dataclass(frozen=True, order=True)
class AckEntry:
    env: str
    kind: str
    object_name: str
    principal: str | None = None
    line: int = 0

    @property
    def key(self) -> str:
        suffix = f":{self.principal}" if self.principal is not None else ""
        return f"{self.env}:{self.kind}:{self.object_name}{suffix}"


def parse_line(text: str, line_number: int = 1, expected_env: str | None = None) -> AckEntry | None:
    """Parse one line and refuse acknowledgements copied from another env."""
    value = text.strip()
    if not value or value.startswith("#"):
        return None
    if "#" in value:
        raise AckFileError(f"line {line_number}: inline comments are not allowed")
    parts = value.split(":")
    if len(parts) < 3 or parts[0] in {*_OBJECT_PARTS, "rollback"}:
        raise AckFileError(
            f"line {line_number}: acknowledgement must begin <env>:<kind>:"
        )
    env, kind = parts[:2]
    if not _ENV.fullmatch(env):
        raise AckFileError(f"line {line_number}: invalid environment {env!r}")
    if expected_env is not None and env != expected_env:
        raise AckFileError(
            f"line {line_number}: acknowledgement env {env!r} does not match checked env {expected_env!r}"
        )
    if kind == "rollback":
        if len(parts) != 3 or not _COMMIT.fullmatch(parts[2]):
            raise AckFileError(
                f"line {line_number}: rollback must be <env>:rollback:<7-64 hex commit>"
            )
        return AckEntry(env, kind, parts[2].lower(), line=line_number)
    if kind not in _OBJECT_PARTS:
        raise AckFileError(f"line {line_number}: unknown acknowledgement kind {kind!r}")
    expected_fields = 3 if kind == "unclassified" else 4
    if len(parts) != expected_fields:
        raise AckFileError(
            f"line {line_number}: expected <env>:{kind}:<object>"
            + ("" if kind == "unclassified" else ":<principal>")
        )
    object_name = parts[2]
    object_parts = len(object_name.split("."))
    allowed_parts = _OBJECT_PARTS[kind]
    if object_parts not in allowed_parts or not _OBJECT.fullmatch(object_name):
        allowed = ", ".join(map(str, sorted(allowed_parts)))
        raise AckFileError(
            f"line {line_number}: {kind} object must have {allowed} valid identifier part(s)"
        )
    principal = None if kind == "unclassified" else parts[3]
    if principal is not None and not _PRINCIPAL.fullmatch(principal):
        raise AckFileError(f"line {line_number}: invalid principal {principal!r}")
    return AckEntry(env, kind, object_name.lower(), principal, line_number)


def parse_text(text: str, expected_env: str | None = None) -> tuple[AckEntry, ...]:
    entries: list[AckEntry] = []
    seen: dict[str, int] = {}
    for number, line in enumerate(text.splitlines(), 1):
        entry = parse_line(line, number, expected_env)
        if entry is None:
            continue
        if entry.key in seen:
            raise AckFileError(
                f"line {number}: duplicate acknowledgement {entry.key!r} "
                f"(first declared on line {seen[entry.key]})"
            )
        seen[entry.key] = number
        entries.append(entry)
    return tuple(entries)


def parse_file(path: Path | str, expected_env: str) -> tuple[AckEntry, ...]:
    ack_path = Path(path)
    try:
        return parse_text(ack_path.read_text(), expected_env)
    except OSError as exc:
        raise AckFileError(f"cannot read acknowledgement file {ack_path}: {exc}") from exc


def reconcile(
    entries: Iterable[AckEntry], consequences: Mapping[str, str]
) -> tuple[dict[str, str], tuple[AckEntry, ...]]:
    """Reconcile stale entries once against consequences from *all* checks."""
    consequence_keys = set(consequences)
    matched = {entry.key: consequences[entry.key] for entry in entries if entry.key in consequence_keys}
    unmatched = tuple(entry for entry in entries if entry.key not in consequence_keys)
    return matched, unmatched


def merge_consequences(*checks: Mapping[str, str]) -> dict[str, str]:
    """Combine weakening, reader, unclassified, and other check consequences."""
    merged: dict[str, str] = {}
    for check in checks:
        for key, consequence in check.items():
            if key in merged and merged[key] != consequence:
                merged[key] = f"{merged[key]}; {consequence}"
            else:
                merged[key] = consequence
    return merged


def load_consequences(path: Path | str) -> dict[str, str]:
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in payload.items()
    ):
        raise AckFileError(f"{path}: consequences must be a JSON object of string keys and values")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ack_file", type=Path)
    parser.add_argument("--env", required=True, help="environment being checked")
    parser.add_argument(
        "--consequences", type=Path, action="append", default=[],
        help="JSON consequences from one check (repeatable); every check's file "
        "is merged and stale entries are reconciled once",
    )
    args = parser.parse_args(argv)
    try:
        entries = parse_file(args.ack_file, args.env)
        if not args.consequences:
            for entry in entries:
                print(entry.key)
            print(f"OK: {len(entries)} acknowledgement(s) are valid for {args.env}")
            return 0
        payload = merge_consequences(*(load_consequences(path) for path in args.consequences))
        matched, unmatched = reconcile(entries, payload)
        for key, consequence in matched.items():
            print(f"ACKNOWLEDGED {key}: {consequence}")
        if unmatched:
            for entry in unmatched:
                print(f"ERROR: line {entry.line}: acknowledgement matches nothing: {entry.key}", file=sys.stderr)
            return 1
        print(f"OK: all {len(entries)} acknowledgement(s) match")
        return 0
    except (AckFileError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
