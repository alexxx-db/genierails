#!/usr/bin/env python3
"""Captured Genie agents and who owns their config (design section 5).

    capture        make capture ENV=dev SPACE=<agent>: one read-only GET; writes
                   only envs/dev/agents/<agent>.space.json. No governance
                   drafting, no model call, no Terraform state.
    import-new     after make generate ENV=dev: captures the agents that have a
                   settings file but no capture yet. Never re-captures one.
    restore-ids    release, before apply: an agent whose ID file is gone gets
                   it back from the release receipt (else create title-adopts, #79).
    sync           release, after apply: pushes each captured config to its
                   agent as one full replacement and prints what it overwrote.
                   A rejected field fails the release. Never in dev.
    receipt        release, after it passed: records the commit and each
                   agent's ID in envs/<env>/.release_receipt.json (ignored).
    detach         make detach-agent: refuses unless HEAD is the released
                   commit and that release had the agent detached; then
                   terraform state rm for its exact addresses.
    guard-removals before each workspace plan/apply: refuses an agent that is
                   gone from config while the workspace state still holds it.

Applying commands write only ignored runtime files (state, ID files, the
receipt); only capture and import-new write committed files.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

SHARED = Path(__file__).resolve().parents[1]
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

import agent_files  # noqa: E402
from agent_files import AgentFileError  # noqa: E402

RECEIPT = ".release_receipt.json"
DEV_ENV = "dev"
# Outer fields of a space; never part of the captured payload.
OUTER_FIELDS = ("space_id", "etag", "access_control_list", "permissions", "acl")
SPACE_API = "/api/2.0/genie/spaces/{}"

Getter = Callable[[str, dict], dict]


class AgentError(RuntimeError):
    pass


# ── Payload ──────────────────────────────────────────────────────────────────

def payload_from_response(response: dict) -> dict:
    """The serialized_space of a GET response, minus outer fields; inner IDs are kept."""
    serialized = response.get("serialized_space") if isinstance(response, dict) else None
    if isinstance(serialized, str):
        if not serialized.strip():
            raise AgentError("the Genie response has an empty serialized_space (is the agent still being created?)")
        try:
            payload = json.loads(serialized)
        except json.JSONDecodeError as exc:
            raise AgentError(f"serialized_space is not valid JSON: {exc}") from exc
    elif isinstance(serialized, dict):
        payload = serialized
    else:
        raise AgentError("the Genie response has no serialized_space")
    if not isinstance(payload, dict):
        raise AgentError("serialized_space must hold a JSON object")
    return {key: value for key, value in payload.items() if key not in OUTER_FIELDS}


def render(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_payload(payload: dict, destination: Path) -> bool:
    text = render(payload)
    if destination.is_file() and destination.read_text() == text:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)
    return True


def _ids(items: list) -> list[str] | None:
    keys = [item.get("id") for item in items if isinstance(item, dict)]
    if len(keys) == len(items) and all(isinstance(k, str) for k in keys) and len(set(keys)) == len(keys):
        return keys
    return None


def changed_fields(before: Any, after: Any, prefix: str = "") -> list[str]:
    """Field paths that differ. List items with an "id" are matched by ID."""
    if isinstance(before, dict) and isinstance(after, dict):
        fields = []
        for key in sorted(set(before) | set(after)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in after:
                fields.append(f"{path} (removed)")
            elif key not in before:
                fields.append(f"{path} (added)")
            else:
                fields.extend(changed_fields(before[key], after[key], path))
        return fields
    if isinstance(before, list) and isinstance(after, list):
        old, new = _ids(before), _ids(after)
        if old is not None and new is not None:
            by_old = {item["id"]: item for item in before}
            by_new = {item["id"]: item for item in after}
            fields = []
            for key in sorted(set(by_old) | set(by_new)):
                path = f"{prefix}[id={key}]"
                if key not in by_new:
                    fields.append(f"{path} (removed)")
                elif key not in by_old:
                    fields.append(f"{path} (added)")
                else:
                    fields.extend(changed_fields(by_old[key], by_new[key], path))
            if not fields and old != new:
                fields.append(f"{prefix} (order)")
            return fields
    if before != after:
        return [prefix or "<root>"]
    return []


# ── Workspace access ─────────────────────────────────────────────────────────

def _api(env_dir: Path):
    """The workspace API client from the env's auth.auto.tfvars (or the SDK's defaults)."""
    from databricks.sdk import WorkspaceClient

    auth = agent_files._load_hcl(env_dir / "auth.auto.tfvars")
    kwargs = {}
    for source, target in (("databricks_workspace_host", "host"), ("databricks_client_id", "client_id"),
                           ("databricks_client_secret", "client_secret")):
        if auth.get(source):
            kwargs[target] = auth[source]
    return WorkspaceClient(**kwargs).api_client


def _get_space(api, space_id: str) -> dict:
    return api.do("GET", SPACE_API.format(space_id), query={"include_serialized_space": "true"})


# ── IDs ──────────────────────────────────────────────────────────────────────

def id_file(env_dir: Path, name: str) -> Path:
    return env_dir / f".genie_space_id_{agent_files.space_key(name)}"


def _legacy_id(env_dir: Path, name: str) -> str:
    cfg = agent_files._load_hcl(env_dir / "env.auto.tfvars")
    ids = [str(s.get("genie_space_id") or "") for s in cfg.get("genie_spaces") or []
           if isinstance(s, dict) and s.get("name") == name]
    return ids[0] if len(ids) == 1 else ""


def agent_id(env_dir: Path, name: str) -> str:
    """The agent's ID: its settings file, else its ID file (create path), else ''."""
    if agent_files.settings_path(env_dir, name).is_file():
        configured = agent_files.load(env_dir, name)["genie_space_id"]
    else:
        configured = _legacy_id(env_dir, name)
    if configured:
        return configured
    path = id_file(env_dir, name)
    return path.read_text().strip() if path.is_file() else ""


# ── Authoring: capture ───────────────────────────────────────────────────────

def capture(env_dir: Path, env: str, name: str, get: Getter | None = None) -> bool:
    if env != DEV_ENV:
        raise AgentError(f"capture runs only with ENV={DEV_ENV}: agents are curated in dev and reach "
                         f"{env} through promote-to")
    agent_files.check_name(name)
    space_id = agent_id(env_dir, name)
    if not space_id:
        raise AgentError(
            f"agent {name!r} has no genie_space_id. Create {agent_files.settings_path(env_dir, name)} with\n"
            f'  genie_space_id = "<the agent ID from its URL>"\n  acl_groups     = ["<group>"]')
    if get is None:
        api = _api(env_dir)
        get = lambda path, query: api.do("GET", path, query=query)  # noqa: E731
    # Read-only by contract: one GET, no PATCH fallback.
    payload = payload_from_response(get(SPACE_API.format(space_id), {"include_serialized_space": "true"}))
    destination = agent_files.capture_path(env_dir, name)
    changed = write_payload(payload, destination)
    print(f"{'Captured' if changed else 'Unchanged'}: {name} ({space_id}) -> agents/{destination.name}")
    if changed:
        print("  Review and commit it; make promote-to carries it to the next env.")
    return changed


def import_new(env_dir: Path, env: str, get: Getter | None = None) -> list[str]:
    """Capture every agent with a settings file and an ID but no capture yet."""
    if env != DEV_ENV:
        return []
    missing = [a["name"] for a in agent_files.load_all(env_dir)
               if a["genie_space_id"] and not agent_files.capture_path(env_dir, a["name"]).is_file()]
    for name in missing:
        capture(env_dir, env, name, get)
    return missing


# ── Release ──────────────────────────────────────────────────────────────────

def load_receipt(env_dir: Path) -> dict:
    path = env_dir / RECEIPT
    if not path.is_file():
        return {}
    try:
        receipt = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise AgentError(f"{path}: unreadable release receipt ({exc})") from exc
    return receipt if isinstance(receipt, dict) else {}


def restore_ids(env_dir: Path) -> list[str]:
    """Rewrite lost ID files from the release receipt; never invents an ID."""
    receipt_agents = load_receipt(env_dir).get("agents") or {}
    restored = []
    for agent in agent_files.load_all(env_dir):
        if agent["genie_space_id"] or agent["delete"]:
            continue
        path = id_file(env_dir, agent["name"])
        recorded = str((receipt_agents.get(agent["name"]) or {}).get("genie_space_id") or "")
        if recorded and not (path.is_file() and path.read_text().strip()):
            path.write_text(recorded + "\n")
            restored.append(agent["name"])
            print(f"Recovered {agent['name']}'s agent ID {recorded} from the release receipt ({path.name}).")
    return restored


def sync(env_dir: Path, env: str, api=None) -> dict[str, list[str]]:
    """Push every captured config to its agent: full replacement, one PATCH each."""
    if env == DEV_ENV:
        print("Genie agent config: dev's is never overwritten (curate it in the UI, then make capture).")
        return {}
    reports: dict[str, list[str]] = {}
    for agent in agent_files.load_all(env_dir):
        name = agent["name"]
        if not agent_files.capture_path(env_dir, name).is_file():
            continue
        if agent["delete"] or agent["detached"]:
            print(f"Genie agent {name}: {'deleted' if agent['delete'] else 'detached'}; config not pushed.")
            continue
        space_id = agent_id(env_dir, name)
        if not space_id:
            raise AgentError(f"agent {name!r} has no ID after apply (no genie_space_id and no "
                             f"{id_file(env_dir, name).name}); its config was not pushed")
        desired = json.loads(agent_files.capture_path(env_dir, name).read_text())
        api = api or _api(env_dir)
        try:
            current = payload_from_response(_get_space(api, space_id))
        except AgentError:
            current = {}  # a just-created agent may not serialize yet: report every field
        except Exception as exc:
            raise AgentError(f"could not read agent {name!r} ({space_id}) before overwriting it: {exc}") from exc
        report = changed_fields(current, desired)
        # One full replacement; no retry that drops a field the API rejects.
        try:
            response = api.do("PATCH", SPACE_API.format(space_id),
                              body={"serialized_space": json.dumps(desired, separators=(",", ":"))})
        except Exception as exc:
            raise AgentError(f"Genie rejected the captured config for {name!r} ({space_id}); nothing was "
                             f"skipped, so the release fails: {exc}") from exc
        returned = str((response or {}).get("space_id") or space_id)
        if returned != space_id:
            raise AgentError(f"Genie answered for agent {returned}, not {space_id}, while updating {name!r}")
        reports[name] = report
        print(f"Genie agent {name} ({space_id}): config replaced from agents/{name}.space.json")
        for field in report:
            print(f"  overwrote {field}")
        if not report:
            print("  no field changed")
    return reports


def delete_agents(env_dir: Path, env: str, api=None) -> list[str]:
    """delete = true on an agent configured by ID deletes it (create-path agents: Terraform trashes them)."""
    deleted = []
    for agent in agent_files.load_all(env_dir):
        if not agent["delete"] or not agent["genie_space_id"]:
            continue
        api = api or _api(env_dir)
        api.do("DELETE", SPACE_API.format(agent["genie_space_id"]))
        deleted.append(agent["name"])
        print(f"Genie agent {agent['name']} ({agent['genie_space_id']}): deleted (delete = true).")
    return deleted


def _git(env_dir: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(env_dir), *args], check=True, text=True,
                          capture_output=True).stdout.strip()


def head_commit(env_dir: Path) -> str:
    try:
        return _git(env_dir, "rev-parse", "HEAD")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise AgentError(f"{env_dir} is not in a git checkout ({exc})") from exc


def tree_clean(env_dir: Path) -> bool:
    try:
        return _git(env_dir, "status", "--porcelain", "--untracked-files=no") == ""
    except (OSError, subprocess.CalledProcessError):
        return False


def write_receipt(env_dir: Path, env: str, commit: str | None = None) -> dict:
    agents = {}
    for agent in agent_files.load_all(env_dir):
        agents[agent["name"]] = {
            "genie_space_id": agent_id(env_dir, agent["name"]),
            "key": agent_files.space_key(agent["name"]),
            "acl_groups": agent["acl_groups"],
            "detached": agent["detached"],
            "delete": agent["delete"],
        }
    receipt = {
        "env": env,
        "commit": commit or head_commit(env_dir),
        "clean_tree": tree_clean(env_dir),
        "released_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agents": agents,
    }
    (env_dir / RECEIPT).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(f"Release receipt: {env_dir.name}/{RECEIPT} (commit {receipt['commit'][:12]}, {len(agents)} agent(s))")
    return receipt


# ── Operations: detach ───────────────────────────────────────────────────────

def _run(runner: Path, env_dir: Path, env: str, *args: str) -> str:
    process_env = dict(os.environ, LAYER_ENV_DIR=str(env_dir))
    return subprocess.run([str(runner), "workspace", env, *args], env=process_env, check=True,
                          text=True, capture_output=True).stdout


def agent_addresses(state_lines: list[str], agent: dict) -> list[str]:
    """The agent's exact module.workspace addresses in a state listing."""
    wanted = {f'module.workspace.{kind}.{resource}["{key}"]'
              for kind in ("null_resource", "terraform_data")
              for resource in agent_files.STATE_RESOURCES
              for key in agent_files.agent_keys(agent)}
    return [line.strip() for line in state_lines if line.strip() in wanted]


def detach(env_dir: Path, env: str, name: str, runner: Path, *, commit: str | None = None,
           clean: bool | None = None) -> list[str]:
    path = agent_files.settings_path(env_dir, name)
    if not path.is_file():
        raise AgentError(f"{path} not found: detach-agent works on an agent file")
    agent = agent_files.load(env_dir, name)
    if not agent["detached"]:
        raise AgentError(
            f"{name} is not detached. Step 1 is a PR that sets acl_groups = [] and detached = true in "
            f"agents/{name}.auto.tfvars, released normally (it revokes CAN_RUN); then run detach-agent.")
    receipt = load_receipt(env_dir)
    if not receipt:
        raise AgentError(f"no release receipt in {env_dir}: release the detached = true change first")
    head = commit or head_commit(env_dir)
    if receipt.get("commit") != head:
        raise AgentError(f"HEAD {head[:12]} is not the last released commit "
                         f"{str(receipt.get('commit'))[:12]}: check out the released commit and re-run")
    if not (tree_clean(env_dir) if clean is None else clean):
        raise AgentError("the checkout has uncommitted changes: run detach-agent from a clean checkout of "
                         "the released commit")
    released = (receipt.get("agents") or {}).get(name) or {}
    if released.get("detached") is not True or released.get("acl_groups") != []:
        raise AgentError(f"the last release ({head[:12]}) did not have {name} detached with acl_groups = []; "
                         "release that change first")
    addresses = agent_addresses(_run(runner, env_dir, env, "state-list").splitlines(), agent)
    for address in addresses:
        _run(runner, env_dir, env, "state-rm", address)
        print(f"  forgot {address}")
    print(f"Detached {name}: {len(addresses)} state address(es) removed; the agent and its conversations stay.")
    print(f"  Next: a PR that removes agents/{name}.* — its plan must show no destroy.")
    return addresses


# ── Guard ────────────────────────────────────────────────────────────────────

def configured_keys(env_dir: Path) -> set[str]:
    """Every key the workspace root may use for a configured (or deleting) agent."""
    cfg = agent_files._load_hcl(env_dir / "env.auto.tfvars")
    keys: set[str] = set()
    for space in cfg.get("genie_spaces") or []:
        if isinstance(space, dict):
            keys |= agent_files.agent_keys({"name": str(space.get("name") or ""),
                                            "genie_space_id": str(space.get("genie_space_id") or "")})
    title = str(cfg.get("genie_space_title") or "")
    if title or cfg.get("genie_space_id"):
        keys.add(agent_files.space_key(title or "Genie Space"))
    for agent in agent_files.load_all(env_dir):
        keys |= agent_files.agent_keys(agent)
    return keys


def guard_removals(env_dir: Path) -> None:
    cfg = agent_files._load_hcl(env_dir / "env.auto.tfvars")
    if cfg.get("governance_mode", "legacy") != "deterministic" and not agent_files.names(env_dir):
        return  # legacy envs keep their remove-means-destroy behaviour
    held = agent_files.state_keys(env_dir / "terraform.tfstate")
    configured = configured_keys(env_dir)
    gone = sorted(key for key in held
                  if key not in configured and key.split("--", 1)[0] not in configured)
    if gone:
        raise AgentError(
            "agent(s) removed from config while the workspace state still holds them: " + ", ".join(gone) + ".\n"
            "  Put the agent file back, then detach it in two steps: a PR with acl_groups = [] and\n"
            "  detached = true, released; then make detach-agent ENV=<env> SPACE=<agent>.\n"
            "  To delete the agent instead, set delete = true.")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("capture", "import-new", "restore-ids", "sync", "receipt",
                                            "detach", "guard-removals"))
    parser.add_argument("--env-dir", type=Path, required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--space", default="")
    parser.add_argument("--runner", type=Path)
    args = parser.parse_args(argv)
    env_dir = args.env_dir
    try:
        if args.command == "capture":
            if not args.space:
                raise AgentError('SPACE="<agent>" is required')
            capture(env_dir, args.env, args.space)
        elif args.command == "import-new":
            import_new(env_dir, args.env)
        elif args.command == "restore-ids":
            restore_ids(env_dir)
        elif args.command == "sync":
            sync(env_dir, args.env)
            delete_agents(env_dir, args.env)
        elif args.command == "receipt":
            write_receipt(env_dir, args.env)
        elif args.command == "detach":
            if not args.space or not args.runner:
                raise AgentError('detach needs SPACE="<agent>" and --runner')
            detach(env_dir, args.env, args.space, args.runner)
        else:
            guard_removals(env_dir)
    except (AgentError, AgentFileError, OSError, subprocess.CalledProcessError) as exc:
        detail = (getattr(exc, "stderr", "") or "").strip()
        print(f"ERROR: {args.command}: {exc}" + (f"\n{detail}" if detail else ""), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
