#!/usr/bin/env python3
"""promote-to's agent step: carry captured agents from the source env to the target.

It reads committed source and target files and never live Databricks. Before
seeding a missing target agent file it also reads the target's last release
receipt, so a removed detached agent cannot be silently recreated.

- agents/<agent>.space.json is promote-to output: rewritten every time from
  the source capture through remap_payload (JSON-aware: catalog names and the
  warehouse ID are replaced on identifier boundaries inside string values,
  never in keys or prose; every item ID is kept).
- agents/<agent>.auto.tfvars is env-owned: the first promotion seeds it
  (acl_groups from the source, genie_space_id empty because the target's ID
  lives in its state and release receipt); later promotions keep it and print
  how its acl_groups differ from the source's.

    agent_promote.py --source-dir envs/dev --dest-dir envs/prod --catalog-map dev_cat=prod_cat
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SHARED = Path(__file__).resolve().parents[1]
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

import agent_files  # noqa: E402
from agent_files import AgentFileError  # noqa: E402
from scripts.agent_ownership import AgentError, load_receipt, render  # noqa: E402
from scripts.saved_settings import catalog_map_dict  # noqa: E402

# A catalog is an identifier when it starts a dotted name: not preceded by an
# identifier character, a dot (then it is a schema or table name) or a
# backtick-quoted run, and followed by a dot (`cat`.x or cat.x).
_IDENT = r"A-Za-z0-9_"


def _catalog_pattern(catalog: str) -> re.Pattern:
    name = re.escape(catalog)
    return re.compile(rf"(?<![{_IDENT}.`$])(?:`{name}`|{name})(?=\.)")


def _warehouse_pattern(warehouse: str) -> re.Pattern:
    return re.compile(rf"(?<![{_IDENT}-]){re.escape(warehouse)}(?![{_IDENT}-])")


def remap_string(value: str, catalogs: dict[str, str], warehouses: dict[str, str]) -> str:
    if value in catalogs:
        return catalogs[value]
    if value in warehouses:
        return warehouses[value]
    for source in sorted(catalogs, key=len, reverse=True):
        destination = catalogs[source]
        value = _catalog_pattern(source).sub(
            lambda m: f"`{destination}`" if m.group(0).startswith("`") else destination, value)
    for source, destination in warehouses.items():
        value = _warehouse_pattern(source).sub(destination, value)
    return value


def remap_payload(value: Any, catalogs: dict[str, str], warehouses: dict[str, str] | None = None) -> Any:
    """Remap string values only; keys, numbers, booleans and prose stay as they are."""
    warehouses = warehouses or {}
    if isinstance(value, dict):
        return {key: remap_payload(item, catalogs, warehouses) for key, item in value.items()}
    if isinstance(value, list):
        return [remap_payload(item, catalogs, warehouses) for item in value]
    if isinstance(value, str):
        return remap_string(value, catalogs, warehouses)
    return value


def _hcl_list(values: list[str]) -> str:
    return "[" + ", ".join(json.dumps(v) for v in values) + "]"


def seed_settings(agent: dict, catalogs: dict[str, str]) -> str:
    lines = [
        f"# {agent['name']} in this env. Env-owned: promote-to wrote it once and never overwrites it.",
        "# The agent's ID lives in this env's Terraform state and release receipt, not here.",
        'genie_space_id = ""',
        f"acl_groups     = {_hcl_list(agent['acl_groups'] or [])}",
    ]
    explicit = agent_files._load_hcl(agent_files.settings_path(agent["_dir"], agent["name"])).get("uc_tables")
    if explicit:
        lines.append(f"uc_tables      = {_hcl_list([remap_string(t, catalogs, {}) for t in explicit])}")
    return "\n".join(lines) + "\n"


def warehouse_map(source_dir: Path, dest_dir: Path) -> dict[str, str]:
    source = str(agent_files._load_hcl(source_dir / "env.auto.tfvars").get("sql_warehouse_id") or "").strip()
    dest = str(agent_files._load_hcl(dest_dir / "env.auto.tfvars").get("sql_warehouse_id") or "").strip()
    return {source: dest} if source and dest and source != dest else {}


def refuse_detached_recreation(source_agents: list[dict], dest_dir: Path) -> None:
    """Refuse before writing when promotion would resurrect a detached target agent.

    The release receipt is runtime state, but it is the durable record left by
    the two-step detach flow after the committed agent file is removed.  An
    absent target file is therefore not a first promotion when that record says
    the target agent was detached.
    """
    missing = {
        agent["name"]
        for agent in source_agents
        if agent_files.capture_path(agent["_dir"], agent["name"]).is_file()
        and not agent_files.settings_path(dest_dir, agent["name"]).is_file()
    }
    if not missing:
        return
    released = (load_receipt(dest_dir, dest_dir.name).get("agents") or {})
    blocked = sorted(
        name for name in missing
        if (released.get(name) or {}).get("detached") is True
        or (released.get(name) or {}).get("lifecycle") == "detached"
    )
    if blocked:
        names = ", ".join(repr(name) for name in blocked)
        raise AgentFileError(
            f"{names} was removed after it was released detached in {dest_dir.name}; refusing to recreate it "
            f"from the active source. Keep it detached, or make a deliberate {dest_dir.name}-side lifecycle "
            "change; nothing was written"
        )


def promote(source_dir: Path, dest_dir: Path, catalogs: dict[str, str]) -> list[str]:
    """Write the target's agent files; returns the agents carried."""
    source_agents = agent_files.load_all(source_dir)
    if not source_agents:
        return []
    source_agents = [agent | {"_dir": source_dir} for agent in source_agents]
    refuse_detached_recreation(source_agents, dest_dir)
    # A target still in the legacy layout keeps these agents inline, with its
    # own (env-owned) acl_groups; carrying them as files would replace those
    # silently. Moving them is the explicit, reviewed make split-agent-files.
    inline = {agent_files.space_key(str(space.get("name") or ""))
              for space in agent_files._load_hcl(dest_dir / "env.auto.tfvars").get("genie_spaces") or []
              if isinstance(space, dict) and space.get("name")}
    clash = sorted(agent["name"] for agent in source_agents if agent_files.space_key(agent["name"]) in inline)
    if clash:
        raise AgentFileError(
            f"{dest_dir.name} still has {', '.join(repr(n) for n in clash)} inline in env.auto.tfvars "
            f"genie_spaces, while {source_dir.name} keeps them in agents/ files. Run make split-agent-files "
            f"ENV={dest_dir.name}, review and commit it, then promote again; nothing was changed")
    warehouses = warehouse_map(source_dir, dest_dir)
    folder = agent_files.agents_dir(dest_dir)
    folder.mkdir(parents=True, exist_ok=True)
    carried = []
    for agent in source_agents:
        name = agent["name"]
        capture = agent_files.capture_path(source_dir, name)
        if not capture.is_file():
            print(f"  {name}: not captured in {source_dir.name} yet (make capture ENV={source_dir.name} "
                  f'SPACE="{name}"); not promoted')
            continue
        payload = json.loads(capture.read_text())
        (folder / capture.name).write_text(render(remap_payload(payload, catalogs, warehouses)))
        settings = agent_files.settings_path(dest_dir, name)
        if not settings.is_file():
            settings.write_text(seed_settings(agent, catalogs))
            print(f"  {name}: promoted; seeded agents/{settings.name} (acl_groups from {source_dir.name})")
        else:
            kept = agent_files.load(dest_dir, name)["acl_groups"]
            note = "" if kept == agent["acl_groups"] else (
                f"; {dest_dir.name} keeps acl_groups {kept} ({source_dir.name} has {agent['acl_groups']})")
            print(f"  {name}: promoted{note}")
        carried.append(name)
    gone = sorted(set(agent_files.names(dest_dir)) - {a["name"] for a in source_agents})
    for name in gone:
        print(f"  {name}: only in {dest_dir.name}; kept (remove it with the two-step detach, or delete = true)")
    return carried


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--dest-dir", type=Path, required=True)
    parser.add_argument("--catalog-map", default="")
    args = parser.parse_args(argv)
    try:
        catalogs = catalog_map_dict(args.catalog_map) if args.catalog_map.strip() else {}
        if agent_files.names(args.source_dir):
            print(f"=== Promote agents ({args.source_dir.name} -> {args.dest_dir.name}) ===")
        promote(args.source_dir, args.dest_dir, catalogs)
    except (AgentError, AgentFileError, ValueError, OSError) as exc:
        print(f"ERROR: promote-to agents: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
