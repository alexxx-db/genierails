#!/usr/bin/env python3
"""Per-agent files: envs/<env>/agents/<agent>.auto.tfvars (+ <agent>.space.json).

One loader for every reader. Terraform gets the merge as a var-file that
terraform_layer.sh writes before each run; validate, table_readers and the
promotion remap call merged_config() directly, so all of them see the same
genie_spaces list.

An agent file holds:

    genie_space_id   = ""        # dev: the agent's ID. prod (any promoted
                                 # deterministic env): must stay "" -- kept in
                                 # state and the release receipt, never in git
    acl_groups       = ["analysts"]
    detached         = false     # true (with acl_groups = []) revokes CAN_RUN;
                                 # then make detach-agent forgets it
    delete           = false     # true deletes the agent (and its conversations)
                                 # on the next release; needs the committed ack
                                 # <env>:delete-agent:<key> in envs/<env>/ack.txt
    uc_tables        = []        # optional; else the captured payload's tables
    sql_warehouse_id = ""        # optional per-agent warehouse

The agent's name is the file name. Its captured config, written by make
capture, is <agent>.space.json beside it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import hcl2

AGENTS_DIR = "agents"
SETTINGS_SUFFIX = ".auto.tfvars"
CAPTURE_SUFFIX = ".space.json"
KEYS = {"genie_space_id", "acl_groups", "detached", "delete", "uc_tables", "sql_warehouse_id"}
# A name becomes a file name and a Terraform key: no paths, no quotes.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]*")
# Workspace-root resources keyed by the agent's key (modules/workspace).
STATE_RESOURCES = (
    "genie_space", "genie_space_acls", "genie_space_acls_removal_only",
    "genie_space_config_existing", "genie_space_config", "genie_space_acls_created",
    "genie_space_acls_created_removal_only",
)


class AgentFileError(ValueError):
    pass


def promoted(env_dir: Path) -> bool:
    """A deterministic promote-to target (promote_from set, or prod), as release_gate.env_role says."""
    cfg = _load_hcl(Path(env_dir) / "env.auto.tfvars")
    return cfg.get("governance_mode", "legacy") == "deterministic" and bool(
        cfg.get("promote_from") or Path(env_dir).name == "prod")


def _load_hcl(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with path.open() as handle:
            return hcl2.load(handle)
    except Exception as exc:  # hcl2 raises lark errors
        raise AgentFileError(f"{path}: not valid HCL ({exc})") from exc


def check_name(name: str) -> str:
    if not NAME.fullmatch(name or "") or ".." in name:
        raise AgentFileError(
            f"agent name {name!r} is not allowed: use letters, digits, spaces, '_', '-' or '.', "
            "starting with a letter or digit")
    return name


def agents_dir(env_dir: Path) -> Path:
    return Path(env_dir) / AGENTS_DIR


def settings_path(env_dir: Path, name: str) -> Path:
    return agents_dir(env_dir) / f"{check_name(name)}{SETTINGS_SUFFIX}"


def capture_path(env_dir: Path, name: str) -> Path:
    return agents_dir(env_dir) / f"{check_name(name)}{CAPTURE_SUFFIX}"


def space_key(name: str) -> str:
    """The workspace root's key for a named space (merged_spaces in roots/workspace)."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def names(env_dir: Path) -> list[str]:
    folder = agents_dir(env_dir)
    if not folder.is_dir():
        return []
    return sorted(path.name[: -len(SETTINGS_SUFFIX)] for path in folder.glob(f"*{SETTINGS_SUFFIX}"))


def load(env_dir: Path, name: str) -> dict[str, Any]:
    """One agent's settings, validated, with defaults filled in."""
    path = settings_path(env_dir, name)
    raw = _load_hcl(path)
    unknown = sorted(set(raw) - KEYS)
    if unknown:
        raise AgentFileError(f"{path}: unknown setting(s) {', '.join(unknown)} (allowed: {', '.join(sorted(KEYS))})")
    acl = raw.get("acl_groups")
    if acl is not None and (not isinstance(acl, list) or not all(isinstance(g, str) and g for g in acl)):
        raise AgentFileError(f"{path}: acl_groups must be a list of group names")
    for flag in ("detached", "delete"):
        if not isinstance(raw.get(flag, False), bool):
            raise AgentFileError(f"{path}: {flag} must be true or false")
    tables = raw.get("uc_tables") or []
    if not isinstance(tables, list) or not all(isinstance(t, str) and t for t in tables):
        raise AgentFileError(f"{path}: uc_tables must be a list of table names")
    agent = {
        "name": name,
        "genie_space_id": str(raw.get("genie_space_id") or "").strip(),
        "acl_groups": list(acl) if acl is not None else None,
        "detached": bool(raw.get("detached", False)),
        "delete": bool(raw.get("delete", False)),
        "uc_tables": list(tables),
        "sql_warehouse_id": str(raw.get("sql_warehouse_id") or "").strip(),
    }
    if agent["genie_space_id"] and promoted(env_dir):
        raise AgentFileError(
            f"{path}: genie_space_id must be empty in {Path(env_dir).name}: a promoted env's agent IDs live in "
            "its Terraform state and release receipt, never in git (to adopt an existing agent, write its ID "
            f"into {Path(env_dir).name}/.genie_space_id_{space_key(name)}; release-plan then shows it)")
    if agent["detached"] and agent["delete"]:
        raise AgentFileError(f"{path}: set detached or delete, not both")
    if agent["detached"] and agent["acl_groups"] != []:
        raise AgentFileError(f"{path}: a detached agent must have acl_groups = [] (detaching revokes CAN_RUN first)")
    if not agent["uc_tables"]:
        agent["uc_tables"] = captured_tables(env_dir, name)
    return agent


def captured_tables(env_dir: Path, name: str) -> list[str]:
    """The tables in an agent's captured payload (data_sources.tables[].identifier)."""
    path = capture_path(env_dir, name)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise AgentFileError(f"{path}: not valid JSON ({exc})") from exc
    tables = ((payload.get("data_sources") or {}).get("tables") or []) if isinstance(payload, dict) else []
    return sorted({str(t.get("identifier")) for t in tables if isinstance(t, dict) and t.get("identifier")})


def load_all(env_dir: Path) -> list[dict[str, Any]]:
    env_dir = Path(env_dir)
    result = [load(env_dir, name) for name in names(env_dir)]
    folder = agents_dir(env_dir)
    if folder.is_dir():
        orphans = sorted(p.name[: -len(CAPTURE_SUFFIX)] for p in folder.glob(f"*{CAPTURE_SUFFIX}")
                         if not (folder / (p.name[: -len(CAPTURE_SUFFIX)] + SETTINGS_SUFFIX)).is_file())
        if orphans:
            raise AgentFileError(
                f"{folder}: captured config without settings for {', '.join(orphans)}; "
                f"add agents/<agent>{SETTINGS_SUFFIX} (genie_space_id, acl_groups)")
    return result


def merged_config(env_dir: Path, cfg: dict | None = None) -> dict:
    """env.auto.tfvars with every agent file merged into genie_spaces.

    Agents marked delete = true are left out (the next apply removes them).
    A name defined both in env.auto.tfvars and in an agent file is refused.
    """
    env_dir = Path(env_dir)
    cfg = dict(cfg if cfg is not None else _load_hcl(env_dir / "env.auto.tfvars"))
    spaces = [dict(s) for s in (cfg.get("genie_spaces") or []) if isinstance(s, dict)]
    inline = {str(s.get("name") or "") for s in spaces} - {""}
    for agent in load_all(env_dir):
        entry = _space_entry(agent) | {"detached": agent["detached"]}
        if agent["name"] in inline:
            if entry in spaces:
                continue  # cfg is already a merge
            raise AgentFileError(
                f"agent {agent['name']!r} is defined in env.auto.tfvars genie_spaces and in "
                f"{AGENTS_DIR}/{agent['name']}{SETTINGS_SUFFIX}; keep only the agent file")
        if agent["delete"]:
            continue
        spaces.append(entry)
    if spaces or "genie_spaces" in cfg or names(env_dir):
        cfg["genie_spaces"] = spaces
    return cfg


def _space_entry(agent: dict) -> dict:
    entry = {
        "name": agent["name"],
        "genie_space_id": agent["genie_space_id"],
        "sql_warehouse_id": agent["sql_warehouse_id"],
        "uc_tables": agent["uc_tables"],
    }
    if agent["acl_groups"] is not None:
        entry["acl_groups"] = agent["acl_groups"]
    return entry


def state_keys(env_dir: Path) -> set[str]:
    """Agent keys the env's workspace state holds a module.workspace Genie resource for.

    Read through scripts/state_layout.py (in the checkout, GENIERAILS_STATE_DIR
    or remote state); a state that should exist but can't be read refuses.
    """
    from scripts.state_layout import state_json

    env_dir = Path(env_dir)
    try:
        state = state_json(env_dir, env_dir.name, "workspace") or {}
    except RuntimeError as exc:
        raise AgentFileError(str(exc)) from exc
    resources = state.get("resources") or []
    keys = set()
    for resource in resources:
        if resource.get("module") != "module.workspace" or resource.get("name") not in STATE_RESOURCES:
            continue
        for instance in resource.get("instances") or []:
            key = instance.get("index_key")
            if isinstance(key, str):
                keys.add(key)
    return keys


def agent_keys(agent: dict) -> set[str]:
    """Keys the workspace root may give this agent (plain, or disambiguated by ID)."""
    base = space_key(agent["name"]) or agent["genie_space_id"]
    keys = {base}
    if agent.get("genie_space_id"):
        keys.add(f"{base}--{agent['genie_space_id']}")
    return keys


def terraform_spaces(env_dir: Path, layer: str) -> list[dict] | None:
    """genie_spaces for one Terraform layer, or None when there are no agent files.

    A detached agent stays in the workspace config only while the workspace
    state still holds it (so a release can revoke its CAN_RUN); once
    make detach-agent has forgotten it, it is left out, so Terraform neither
    recreates nor destroys it. The data_access layer keeps it: its tables stay
    governed.
    """
    env_dir = Path(env_dir)
    if not names(env_dir):
        return None
    cfg = merged_config(env_dir)
    in_state = state_keys(env_dir) if layer == "workspace" else set()
    spaces = []
    from_files = set(names(env_dir))
    for space in cfg.get("genie_spaces") or []:
        detached = space.pop("detached", False)
        if detached and layer == "workspace" and not (agent_keys(space) & in_state):
            continue
        if layer == "workspace" and space.get("name") in from_files:
            space["captured"] = capture_path(env_dir, space["name"]).is_file()
        spaces.append(space)
    return spaces


def write_tfvars(env_dir: Path, layer: str, out: Path) -> bool:
    spaces = terraform_spaces(env_dir, layer)
    if spaces is None:
        return False
    out.write_text(json.dumps({"genie_spaces": spaces}, indent=2, sort_keys=True) + "\n")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("tfvars", "validate"))
    parser.add_argument("--env-dir", type=Path, required=True,
                        help="envs/<env> (the workspace env dir, not data_access/)")
    parser.add_argument("--layer", choices=("workspace", "data_access"), default="workspace")
    parser.add_argument("--var-file", dest="var_file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            merged_config(args.env_dir)
            return 0
        if args.var_file is None:
            parser.error("tfvars needs --var-file")
        # Exit 3: no agent files, nothing written.
        return 0 if write_tfvars(args.env_dir, args.layer, args.var_file) else 3
    except AgentFileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
