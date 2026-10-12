#!/usr/bin/env python3
"""Ignored runtime overlay of a deterministic env's generated config (design section 9).

Applying commands (rehearse, release-plan, release, maintain, and the apply
and plan targets they run) never rewrite committed files. In a deterministic
env, everything they re-derive goes to ``envs/<env>/generated/.runtime/``
(ignored, like every ``generated/.*``) instead of ``generated/``:

* ``abac.auto.tfvars`` and ``masking_functions.sql``: the model re-derived
  from live ``class.*`` tags (derive-assignments), with deployed policy names
  kept (below), and the ACL sidecar the same-env promote derives next to it;
* ``base.json``: the digest of the committed files the overlay was made from.
  An overlay whose base no longer matches (a new commit) is ignored until the
  next derive, so a stale live derivation never outlives its inputs;
* ``kept_policy_names.json``: promoted policy name -> deployed name (#68).

``path`` prints the file a reader uses: the overlay's when it is current,
else the committed one. Legacy envs, and ``GENIERAILS_NO_OVERLAY=1`` (the
scratch copy of make check), always get the committed file, so nothing about
them changes.

Kept policy names. promote-to is a pure function of the commit (design
section 9): it always names a promoted policy after its destination catalog.
A policy already deployed under its old name can't be renamed (provider 1.111
renames by delete plus create), so ``kept-names`` maps each promoted name to
the name prod already deploys, from prod's data_access state and a live
listing of the catalog's policies. It fails closed: a listing that can't be
trusted, or a promoted name that collides with a policy GenieRails doesn't
manage, stops the run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Callable, Mapping

SHARED = Path(__file__).resolve().parents[1]
for _path in (SHARED, SHARED / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

OVERLAY = Path("generated") / ".runtime"
FILES = ("abac.auto.tfvars", "masking_functions.sql")
SIDECAR = "genie_space_derived_acl_groups.auto.tfvars"
BASE = "base.json"
KEPT = "kept_policy_names.json"
NO_OVERLAY_ENV = "GENIERAILS_NO_OVERLAY"


class OverlayError(RuntimeError):
    """The overlay can't be built safely; nothing that depends on it may run."""


def _load_hcl(path: Path) -> dict:
    import hcl2

    if not path.is_file():
        return {}
    try:
        return hcl2.loads(path.read_text()) or {}
    except Exception as exc:
        raise OverlayError(f"cannot parse {path}: {exc}") from exc


def deterministic(env_dir: Path) -> bool:
    return _load_hcl(Path(env_dir) / "env.auto.tfvars").get("governance_mode", "legacy") == "deterministic"


def enabled(env_dir: Path) -> bool:
    return os.environ.get(NO_OVERLAY_ENV) != "1" and deterministic(env_dir)


def overlay_dir(env_dir: Path) -> Path:
    return Path(env_dir) / OVERLAY


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def committed_base(env_dir: Path) -> str:
    """Digest of the committed inputs an overlay is derived from."""
    env_dir = Path(env_dir)
    parts = {name: _sha(env_dir / "generated" / name) for name in FILES}
    parts["env.auto.tfvars"] = _sha(env_dir / "env.auto.tfvars")
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def active(env_dir: Path) -> bool:
    """Whether readers use the overlay: deterministic, present and made from these inputs."""
    if not enabled(env_dir):
        return False
    directory = overlay_dir(env_dir)
    try:
        base = json.loads((directory / BASE).read_text()).get("base")
    except (OSError, ValueError, AttributeError):
        return False
    return base == committed_base(env_dir) and (directory / FILES[0]).is_file()


def effective(env_dir: Path, name: str) -> Path:
    """The file a reader of generated/<name> uses."""
    env_dir = Path(env_dir)
    if active(env_dir) and (name in FILES or name == SIDECAR):
        return overlay_dir(env_dir) / name
    return env_dir / "generated" / name


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def begin(env_dir: Path) -> Path:
    """Start a fresh overlay from the committed files; returns its dir."""
    env_dir = Path(env_dir)
    directory = overlay_dir(env_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / BASE).unlink(missing_ok=True)
    for name in FILES + (SIDECAR,):
        (directory / name).unlink(missing_ok=True)
        source = env_dir / "generated" / name
        if source.is_file():
            shutil.copyfile(source, directory / name)
    return directory


def seal(env_dir: Path) -> None:
    """Mark the overlay current for the committed inputs it was made from."""
    _write(overlay_dir(env_dir) / BASE, json.dumps({"base": committed_base(env_dir)}, indent=2) + "\n")


def ensure(env_dir: Path) -> bool:
    """Make sure applying writes land in the overlay (a fresh copy when none is current)."""
    if not enabled(env_dir) or not (Path(env_dir) / "generated" / FILES[0]).is_file():
        return False
    if not active(env_dir):
        begin(env_dir)
        apply_kept_names(env_dir, load_kept_names(env_dir))
        seal(env_dir)
    return True


def digest(env_dir: Path) -> str:
    """Digest of what the effective config holds (manifests record it)."""
    parts = {name: _sha(effective(env_dir, name)) for name in FILES}
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


# -- kept policy names (#68) ---------------------------------------------------

def load_kept_names(env_dir: Path) -> dict[str, str]:
    path = overlay_dir(env_dir) / KEPT
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise OverlayError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise OverlayError(f"{path} must map promoted policy names to deployed names")
    return value


def _catalog_map(env_dir: Path) -> dict[str, str]:
    raw = _load_hcl(Path(env_dir) / "env.auto.tfvars").get("catalog_map") or {}
    if isinstance(raw, list):  # hcl2 may wrap a map in a list
        raw = raw[0] if raw else {}
    return {str(k): str(v) for k, v in dict(raw).items()}


def kept_policy_names(
    policies: list[Mapping], catalog_map: Mapping[str, str],
    state_keys: set[tuple[str, str]] | None, live_names: Callable[[str], set[str] | None],
) -> dict[str, str]:
    """Promoted name -> deployed name, for policies prod deploys under an older name.

    The data_access module names a remote policy ``<catalog>_<name>`` and keys
    it by ``name``. A promoted name whose source-catalog form is in prod's
    state or live listing keeps that form. Raises OverlayError when a catalog
    can't be listed, two older forms are deployed, or the promoted name is
    taken by a policy GenieRails doesn't manage.
    """
    from remap_generated_config import remap_policy_name

    sources: dict[str, list[str]] = {}
    for src, dest in catalog_map.items():
        if src != dest:
            sources.setdefault(dest, []).append(src)
    managed = state_keys or set()
    cache: dict[str, set[str] | None] = {}
    kept: dict[str, str] = {}
    for policy in policies:
        if policy.get("policy_type") != "POLICY_TYPE_COLUMN_MASK":
            continue
        name, catalog = str(policy.get("name") or ""), str(policy.get("catalog") or "")
        older = sorted({remap_policy_name(name, catalog, src) for src in sources.get(catalog, [])} - {name})
        if not older:
            continue
        if catalog not in cache:
            cache[catalog] = live_names(catalog)
        live = cache[catalog]
        if live is None:
            raise OverlayError(
                f"the policies on {catalog} couldn't be listed, so it is unknown whether {name} is deployed "
                f"under an older name; refusing to plan a rename (#68). Check {catalog}'s credentials and retry")
        deployed = [k for k in older if (catalog, k) in managed or f"{catalog}_{k}" in live]
        if len(deployed) > 1:
            raise OverlayError(f"{catalog} deploys {name} under several older names ({', '.join(deployed)})")
        if deployed:
            kept[name] = deployed[0]
        elif f"{catalog}_{name}" in live and (catalog, name) not in managed:
            raise OverlayError(
                f"{catalog} already has a policy named {catalog}_{name} that GenieRails doesn't manage; remove "
                "it or import it into the prod data_access state, then re-run")
    final = [kept.get(str(p.get("name")), str(p.get("name"))) for p in policies]
    duplicates = sorted({n for n in final if final.count(n) > 1})
    if duplicates:
        raise OverlayError("kept policy names would collide: " + ", ".join(duplicates))
    return kept


def apply_kept_names(env_dir: Path, kept: Mapping[str, str]) -> None:
    """Rename the overlay's fgac_policies to their deployed names."""
    from remap_generated_config import _top_level_list_span

    path = overlay_dir(env_dir) / FILES[0]
    if not kept or not path.is_file():
        return
    text = path.read_text()
    span = _top_level_list_span(text, "fgac_policies")
    if span is None:
        return
    _start, bracket, end = span
    section = re.sub(r'(?m)^(\s*name\s*=\s*")([^"]*)(")',
                     lambda m: m.group(1) + kept.get(m.group(2), m.group(2)) + m.group(3),
                     text[bracket:end])
    _write(path, text[:bracket] + section + text[end:])


def build_kept_names(env_dir: Path, *, state_keys=None, live_names=None) -> dict[str, str]:
    """Rebuild the kept-name map for the current overlay and apply it."""
    from remap_generated_config import live_policy_lister, read_state_policy_keys

    env_dir = Path(env_dir)
    if not ensure(env_dir):
        return {}
    catalog_map = _catalog_map(env_dir)
    kept: dict[str, str] = {}
    if catalog_map:
        policies = _load_hcl(overlay_dir(env_dir) / FILES[0]).get("fgac_policies") or []
        # A materialized overlay already carries the previous kept names; work
        # from the promoted form and rename once, from what it holds now.
        reverse = {deployed: promoted for promoted, deployed in load_kept_names(env_dir).items()}
        current = [str(p.get("name")) for p in policies]
        promoted = [dict(p) | {"name": reverse.get(name, name)} for p, name in zip(policies, current)]
        if state_keys is None:
            from state_layout import state_json
            state = state_json(env_dir, env_dir.name, "data_access") or {}
            state_keys = read_state_policy_keys(state=state)
        if live_names is None:
            live_names = live_policy_lister(env_dir / "data_access" / "auth.auto.tfvars")
        kept = kept_policy_names(promoted, catalog_map, state_keys, live_names)
        rename = {name: kept.get(reverse.get(name, name), reverse.get(name, name)) for name in current}
        apply_kept_names(env_dir, {old: new for old, new in rename.items() if old != new})
    _write(overlay_dir(env_dir) / KEPT, json.dumps(kept, indent=2, sort_keys=True) + "\n")
    seal(env_dir)
    for promoted_name, deployed_name in sorted(kept.items()):
        print(f"  kept policy name {deployed_name} (deployed; promoted as {promoted_name})")
    return kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("path", "ensure", "begin", "seal", "kept-names", "digest", "active", "enabled"):
        command = sub.add_parser(name)
        command.add_argument("--env-dir", type=Path, required=True)
        if name == "path":
            command.add_argument("name", choices=FILES + (SIDECAR,))
    args = parser.parse_args(argv)
    try:
        if args.command == "path":
            print(effective(args.env_dir, args.name))
        elif args.command == "active":
            return 0 if active(args.env_dir) else 1
        elif args.command == "enabled":
            print(int(enabled(args.env_dir)))
        elif args.command == "ensure":
            ensure(args.env_dir)
        elif args.command == "begin":
            print(begin(args.env_dir))
        elif args.command == "seal":
            seal(args.env_dir)
        elif args.command == "digest":
            print(digest(args.env_dir))
        else:
            build_kept_names(args.env_dir)
    except OverlayError as exc:
        if args.command == "kept-names":
            # Fail closed: an overlay without its kept names is never used.
            (overlay_dir(args.env_dir) / BASE).unlink(missing_ok=True)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
