#!/usr/bin/env python3
"""Captured Genie configuration and production ownership lifecycle.

The capture command is deliberately read-only: it performs one GET and only
writes the exported ``serialized_space`` payload.  Production sync is the
opposite: the captured payload is authoritative and a rejected PATCH is fatal.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import hcl2


class AgentError(RuntimeError):
    pass


def _load_hcl(path: Path) -> dict[str, Any]:
    return hcl2.load(path.open()) if path.is_file() else {}


def _agent_file(env_dir: Path, name: str) -> Path:
    if not name or name in {".", ".."} or Path(name).name != name:
        raise AgentError("SPACE must be an agent name, not a path")
    return env_dir / "agents" / f"{name}.auto.tfvars"


def _capture_file(env_dir: Path, name: str) -> Path:
    _agent_file(env_dir, name)  # validate the name
    return env_dir / "agents" / f"{name}.space.json"


def agent_settings(env_dir: Path, name: str) -> dict[str, Any]:
    """Load the step-11 sidecar, accepting legacy genie_spaces during migration."""
    sidecar = _agent_file(env_dir, name)
    if sidecar.is_file():
        cfg = _load_hcl(sidecar)
        nested = cfg.get("agent")
        return nested if isinstance(nested, dict) else cfg
    cfg = _load_hcl(env_dir / "env.auto.tfvars")
    matches = [s for s in cfg.get("genie_spaces", []) if (s.get("name") or "") == name]
    if len(matches) != 1:
        raise AgentError(f"agent {name!r} must have exactly one agents/{name}.auto.tfvars file")
    return matches[0]


def _space_id(env_dir: Path, name: str) -> str:
    value = str(agent_settings(env_dir, name).get("genie_space_id") or "").strip()
    if not value:
        raise AgentError(f"agent {name!r} has no genie_space_id in its own config")
    return value


def write_capture(response: dict[str, Any], destination: Path) -> bool:
    serialized = response.get("serialized_space")
    if isinstance(serialized, str):
        try:
            payload = json.loads(serialized)
        except json.JSONDecodeError as exc:
            raise AgentError(f"serialized_space is not valid JSON: {exc}") from exc
    elif isinstance(serialized, dict):
        payload = serialized
    else:
        raise AgentError("Genie response has no serialized_space object")
    if not isinstance(payload, dict):
        raise AgentError("serialized_space must contain a JSON object")
    rendered = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if destination.is_file() and destination.read_text() == rendered:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered)
    return True


def _client(env_dir: Path):
    from databricks.sdk import WorkspaceClient
    auth = _load_hcl(env_dir / "auth.auto.tfvars")
    kwargs = {}
    for source, target in (("databricks_workspace_host", "host"), ("databricks_token", "token"),
                           ("databricks_client_id", "client_id"),
                           ("databricks_client_secret", "client_secret")):
        value = auth.get(source)
        if value:
            kwargs[target] = value
    return WorkspaceClient(**kwargs)


def capture(env_dir: Path, name: str, api_get: Callable[[str, dict], dict] | None = None) -> bool:
    space_id = _space_id(env_dir, name)
    if api_get is None:
        workspace = _client(env_dir)
        api_get = lambda path, query: workspace.api_client.do("GET", path, query=query)
    # No PATCH fallback: capture credentials are read-only by contract.
    response = api_get(f"/api/2.0/genie/spaces/{space_id}", {"include_serialized_space": "true"})
    return write_capture(response, _capture_file(env_dir, name))


def _maps(env_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    cfg = _load_hcl(env_dir / "env.auto.tfvars")
    catalogs = {str(k): str(v) for k, v in (cfg.get("catalog_map") or {}).items()}
    warehouses = {str(k): str(v) for k, v in (cfg.get("warehouse_map") or {}).items()}
    return catalogs, warehouses


def remap_payload(value: Any, catalogs: dict[str, str], warehouses: dict[str, str]) -> Any:
    """Remap JSON values only; keys and substrings/prose are left untouched."""
    if isinstance(value, dict):
        return {key: remap_payload(item, catalogs, warehouses) for key, item in value.items()}
    if isinstance(value, list):
        return [remap_payload(item, catalogs, warehouses) for item in value]
    if not isinstance(value, str):
        return value
    if value in warehouses:
        return warehouses[value]
    result = value
    for source, destination in sorted(catalogs.items(), key=lambda item: -len(item[0])):
        # Catalog identifiers have a following dot. This intentionally does not
        # rewrite a catalog name merely mentioned in natural-language prose.
        result = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(source)}(?=\.)", destination, result)
    return result


def changed_fields(before: Any, after: Any, prefix: str = "") -> list[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        fields = []
        for key in sorted(set(before) | set(after)):
            path = f"{prefix}.{key}" if prefix else key
            fields.extend(changed_fields(before.get(key), after.get(key), path))
        return fields
    if before != after:
        return [prefix or "<root>"]
    return []


def sync_one(env_dir: Path, name: str, api: Any | None = None) -> list[str]:
    capture_path = _capture_file(env_dir, name)
    if not capture_path.is_file():
        raise AgentError(f"missing captured config {capture_path}")
    desired = remap_payload(json.loads(capture_path.read_text()), *_maps(env_dir))
    space_id = _space_id(env_dir, name)
    workspace = api or _client(env_dir).api_client
    live = workspace.do("GET", f"/api/2.0/genie/spaces/{space_id}",
                        query={"include_serialized_space": "true"})
    raw = live.get("serialized_space")
    current = json.loads(raw) if isinstance(raw, str) else (raw or {})
    report = changed_fields(current, desired)
    print(f"Genie overwrite report: {name} ({space_id})")
    if report:
        for field in report:
            print(f"  overwrite {field}")
    else:
        print("  no captured fields changed")
    # One full replacement. No field-dropping retries are permitted.
    try:
        workspace.do("PATCH", f"/api/2.0/genie/spaces/{space_id}",
                     body={"serialized_space": json.dumps(desired, separators=(",", ":"))})
    except Exception as exc:
        raise AgentError(f"Genie rejected the full captured config for {name!r}: {exc}") from exc
    return report


def sync_all(env_dir: Path, api: Any | None = None) -> int:
    captures = sorted((env_dir / "agents").glob("*.space.json"))
    for path in captures:
        sync_one(env_dir, path.name.removesuffix(".space.json"), api)
    return len(captures)


def assemble_sidecars(env_dir: Path) -> int:
    """Build Terraform's transient aggregate from committed per-agent files."""
    files = sorted((env_dir / "agents").glob("*.auto.tfvars"))
    output = env_dir / "zz_agents.auto.tfvars.json"
    if not files:
        output.unlink(missing_ok=True)
        return 0
    spaces = []
    for path in files:
        name = path.name.removesuffix(".auto.tfvars")
        cfg = _load_hcl(path)
        cfg = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else cfg
        spaces.append({
            "name": name,
            "genie_space_id": str(cfg.get("genie_space_id") or ""),
            "sql_warehouse_id": str(cfg.get("sql_warehouse_id") or ""),
            "uc_tables": list(cfg.get("uc_tables") or []),
            "acl_groups": list(cfg.get("acl_groups") or []),
        })
    output.write_text(json.dumps({"genie_spaces": spaces}, indent=2, sort_keys=True) + "\n")
    return len(spaces)


def _run(runner: Path, env_dir: Path, env: str, *args: str, capture_output=False):
    process_env = os.environ.copy()
    process_env["LAYER_ENV_DIR"] = str(env_dir)
    return subprocess.run([str(runner), "workspace", env, *args], env=process_env,
                          text=True, capture_output=capture_output, check=True)


STATE_SUFFIXES = ("genie_space", "genie_space_acls", "genie_space_acls_removal_only",
                  "genie_space_config_existing", "genie_space_config_created",
                  "genie_space_acls_created", "genie_space_acls_created_removal_only")


def guard_removals(env_dir: Path, env: str, runner: Path) -> None:
    """Refuse an agent disappearing from config while its state is attached."""
    configured = {p.name.removesuffix(".auto.tfvars") for p in (env_dir / "agents").glob("*.auto.tfvars")}
    legacy = _load_hcl(env_dir / "env.auto.tfvars").get("genie_spaces") or []
    configured.update(str(item.get("name") or "") for item in legacy)
    detached = {p.name.removesuffix(".detached") for p in (env_dir / "agents").glob("*.detached")}
    state = _run(runner, env_dir, env, "state-list", capture_output=True).stdout.splitlines()
    missing = set()
    for address in state:
        if not any(token in address for token in STATE_SUFFIXES):
            continue
        match = re.search(r'\["([^"]+)"\]', address)
        if match and match.group(1) not in configured and match.group(1) not in detached:
            missing.add(match.group(1))
    if missing:
        raise AgentError("agent(s) removed from config before detach-agent: " + ", ".join(sorted(missing)))


def detach(env_dir: Path, env: str, name: str, runner: Path, api: Any | None = None) -> None:
    settings = agent_settings(env_dir, name)
    space_id = _space_id(env_dir, name)
    workspace = api or _client(env_dir).api_client
    workspace.do("PUT", f"/api/2.0/permissions/genie/{space_id}", body={"access_control_list": []})
    acl = workspace.do("GET", f"/api/2.0/permissions/genie/{space_id}")
    direct = [p for ace in acl.get("access_control_list", []) for p in ace.get("all_permissions", [])
              if not p.get("inherited") and p.get("permission_level") == "CAN_RUN"]
    if direct:
        raise AgentError(f"CAN_RUN still exists on {name!r}; refusing state detach")
    state = _run(runner, env_dir, env, "state-list", capture_output=True).stdout.splitlines()
    key_candidates = {name, re.sub(r"[^0-9A-Za-z_]", "_", name).lower()}
    addresses = [address for address in state if any(f'["{key}"]' in address for key in key_candidates)
                 and any(token in address for token in STATE_SUFFIXES)]
    for address in addresses:
        _run(runner, env_dir, env, "state-rm", address)
    marker = env_dir / "agents" / f"{name}.detached"
    marker.write_text(space_id + "\n")
    sidecar = _agent_file(env_dir, name)
    if sidecar.is_file():
        sidecar.unlink()
        assemble_sidecars(env_dir)
    plan = _run(runner, env_dir, env, "plan", "-no-color", capture_output=True).stdout
    if re.search(r"(^|\n)\s*-\s+.*(?:destroy|forces replacement)|Plan:.*\b[1-9]\d* to destroy", plan):
        raise AgentError("post-detach plan contains a destroy/replace; state was detached but config was kept")
    print(f"Detached {name}: revoked CAN_RUN, removed {len(addresses)} state address(es), plan has no destroy.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("capture", "sync-prod", "detach", "assemble", "guard-removals"))
    parser.add_argument("--env-dir", type=Path, required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--space", default="")
    parser.add_argument("--runner", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "capture":
            if args.env != "dev":
                raise AgentError("capture is allowed only with ENV=dev")
            print("Captured." if capture(args.env_dir, args.space) else "Capture unchanged.")
        elif args.command == "sync-prod":
            if args.env != "prod":
                return 0
            print(f"Authoritatively updated {sync_all(args.env_dir)} captured Genie agent(s).")
        elif args.command == "detach":
            if not args.runner:
                raise AgentError("detach requires --runner")
            detach(args.env_dir, args.env, args.space, args.runner)
        elif args.command == "assemble":
            assemble_sidecars(args.env_dir)
        else:
            if not args.runner:
                raise AgentError("guard-removals requires --runner")
            guard_removals(args.env_dir, args.env, args.runner)
    except (AgentError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
