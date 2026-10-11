#!/usr/bin/env python3
"""Versioned policy generations and the staged-apply hooks for deterministic governance.

This is the step-5 side of the interface ``scripts/staged_policy_apply.py``
(step 6) drives. A **generation** is one deterministic model: its column-mask
policies (with principals), gr_treatment tag values and functions, identified
by ``governance_policies.model_fingerprint``. Function, policy and treatment
value names are content-versioned (see governance_policies.py), so two
generations never fight over one name.

Hooks are argv executables in ``scripts/policy_hooks/`` (``create``,
``switch``, ``retire``). Each renders one stage into the data_access layer,
plans it, refuses any destroy the stage doesn't allow, and applies exactly that
plan. They read:

* ``GENIERAILS_ENV_DIR``: the env (``envs/<env>``); required.
* ``GENIERAILS_ENV_NAME``: defaults to the env dir's name.
* ``GENIERAILS_POLICY_FINGERPRINT``: must equal ``fingerprint`` of the
  generated config, so a hook never applies a model the controller didn't
  record.
* ``GENIERAILS_OLD_POLICY_FINGERPRINT``: the active generation, kept live.
* ``GENIERAILS_RETIRE_FINGERPRINT``: a generation an earlier release made
  obsolete; kept live until ``retire`` removes it.

Stages (every one keeps all retained generations' functions):

* ``create``: replacement policies are added; retained policies keep their
  principals; every column keeps its old treatment value. Nothing is removed.
* ``switch``: columns are retagged to the replacement values and same-named
  policies take their new principals (tier moves). Retained policies stay.
* ``retire``: the retire generation's policies leave; its functions stay
  (functions are only dropped when the layer is destroyed).

Snapshots of every generation a hook applies are kept next to the layer's
state, in ``<env>/data_access/.policy_generations/<fingerprint>.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SHARED = Path(__file__).resolve().parents[1]
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from governance_policies import (  # noqa: E402
    TAG_KEY, model_fingerprint, parse_treatment_value, replace_sql_library,
    sql_library_block,
)

STAGES = ("create", "switch", "retire")
RUNNER = SHARED / "scripts" / "terraform_layer.sh"
LEDGER = Path(".policy_generations")
# Resources a staged apply may never destroy: a live policy, a treatment tag
# (a column would lose its mask), or the resource whose destroy drops functions.
PROTECTED_TYPES = {
    "databricks_policy_info", "databricks_entity_tag_assignment", "terraform_data.masking_functions_drop",
}


class StageError(RuntimeError):
    pass


def _load(path: Path) -> dict:
    import hcl2
    return hcl2.loads(path.read_text()) if path.is_file() else {}


def desired_generation(env_dir: Path) -> dict:
    """The generation the generated config describes."""
    cfg = _load(env_dir / "generated" / "abac.auto.tfvars")
    sql_path = env_dir / "generated" / "masking_functions.sql"
    functions = sql_library_block(sql_path.read_text() if sql_path.is_file() else "")
    masks = [p for p in cfg.get("fgac_policies") or [] if p.get("policy_type") == "POLICY_TYPE_COLUMN_MASK"]
    tags = {
        item["entity_name"]: item["tag_value"] for item in cfg.get("tag_assignments") or []
        if item.get("entity_type") == "columns" and item.get("tag_key") == TAG_KEY
    }
    undeterministic = sorted(v for v in tags.values() if not parse_treatment_value(v))
    if undeterministic:
        raise StageError(
            "generated config is not a deterministic generation (treatment values "
            f"{undeterministic[:3]}); run make generate or derive-assignments in deterministic mode"
        )
    return {
        "fingerprint": model_fingerprint(cfg, functions),
        "fgac_policies": masks, "tags": tags, "functions_sql": functions,
    }


def ledger_dir(env_dir: Path) -> Path:
    return env_dir / "data_access" / LEDGER


def save_snapshot(env_dir: Path, generation: dict) -> Path:
    path = ledger_dir(env_dir) / f"{generation['fingerprint']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(generation, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return path


def load_snapshot(env_dir: Path, fingerprint: str) -> dict | None:
    if not fingerprint:
        return None
    path = ledger_dir(env_dir) / f"{fingerprint}.json"
    if not path.is_file():
        raise StageError(
            f"no snapshot of live generation {fingerprint} in {path.parent}; refusing to render "
            "a graph that could destroy its policies"
        )
    return json.loads(path.read_text())


def _merge_functions(*blocks: str) -> str:
    """The union of several generations' function blocks, one statement each."""
    statements: set[tuple[str, str, str]] = set()
    for block in blocks:
        catalog = schema = ""
        for raw in block.split(";\n"):
            stmt = raw.strip().rstrip(";")
            if stmt.upper().startswith("USE CATALOG "):
                catalog = stmt
            elif stmt.upper().startswith("USE SCHEMA "):
                schema = stmt
            elif stmt:
                # CREATE OR REPLACE of identical text is idempotent.
                statements.add((catalog, schema, stmt))
    out, current = [], None
    for catalog, schema, stmt in sorted(statements):
        if (catalog, schema) != current:
            out.append(f"{catalog};\n{schema};")
            current = (catalog, schema)
        out.append(stmt + ";")
    return "\n\n".join(out) + ("\n" if out else "")


def render_stage(stage: str, desired: dict, old: dict | None, retire: dict | None) -> dict:
    """Policies, tags and functions the data_access layer holds for ``stage``."""
    if stage not in STAGES:
        raise StageError(f"unknown stage {stage!r}")
    kept = [g for g in (old, retire if stage != "retire" else None) if g]
    policies: dict[str, dict] = {}
    if stage == "create":
        # Retained definitions win: create changes no live policy.
        for generation in [desired] + kept:
            for policy in generation["fgac_policies"]:
                policies[policy["name"]] = policy
        tags = dict((old or {}).get("tags") or {})
    else:
        # Desired definitions win: switch moves principals in place.
        for generation in list(reversed(kept)) + [desired]:
            for policy in generation["fgac_policies"]:
                policies[policy["name"]] = policy
        tags = dict(desired["tags"])
    functions = _merge_functions(*(g["functions_sql"] for g in [desired, old, retire] if g))
    return {"fgac_policies": [policies[name] for name in sorted(policies)], "tags": tags,
            "functions_sql": functions}


def write_stage(layer_dir: Path, rendered: dict) -> None:
    """Write a rendered stage over the data_access layer's split config."""
    from generate_abac import (
        _find_bracket_section, _render_fgac_policy_block, _render_tag_assignment_block,
        _replace_bracket_section,
    )

    path = layer_dir / "abac.auto.tfvars"
    text = path.read_text()
    cfg = _load(path)
    assignments = [
        item for item in cfg.get("tag_assignments") or []
        if not (item.get("entity_type") == "columns" and item.get("tag_key") == TAG_KEY)
    ] + [
        {"entity_type": "columns", "entity_name": column, "tag_key": TAG_KEY, "tag_value": value}
        for column, value in sorted(rendered["tags"].items())
    ]
    policies = [
        p for p in cfg.get("fgac_policies") or [] if p.get("policy_type") != "POLICY_TYPE_COLUMN_MASK"
    ] + rendered["fgac_policies"]
    for section, items, render in (
        ("tag_assignments", assignments, _render_tag_assignment_block),
        ("fgac_policies", policies, _render_fgac_policy_block),
    ):
        if _find_bracket_section(text, section) is None:
            text = text.rstrip() + f"\n\n{section} = [\n]\n"
        text = _replace_bracket_section(text, section, [render(item) for item in items])
    path.write_text(text)
    sql_path = layer_dir / "masking_functions.sql"
    sql = sql_path.read_text() if sql_path.is_file() else ""
    sql_path.write_text(replace_sql_library(sql, rendered["functions_sql"]))


def plan_violations(plan: dict, stage: str, allowed_policy_deletes: set[str] = frozenset()) -> list[str]:
    """Destroys in a plan that the stage may not perform."""
    problems = []
    for change in plan.get("resource_changes") or []:
        if "delete" not in (change.get("change") or {}).get("actions") or []:
            continue
        address = str(change.get("address") or "")
        rtype = str(change.get("type") or "")
        key = str(change.get("index") or "")
        protected = rtype in PROTECTED_TYPES or any(address.endswith(t) for t in PROTECTED_TYPES if "." in t)
        if not protected:
            continue
        if stage == "retire" and rtype == "databricks_policy_info" and key in allowed_policy_deletes \
                and "create" not in change["change"]["actions"]:
            continue
        problems.append(f"{address}: {'/'.join(change['change']['actions'])}")
    return problems


def _runner(env_name: str, layer_dir: Path, *args: str, capture: bool = False) -> str:
    result = subprocess.run(
        [str(RUNNER), "data_access", env_name, *args],
        env=os.environ | {"LAYER_ENV_DIR": str(layer_dir)},
        text=True, stdout=subprocess.PIPE if capture else None, check=False,
    )
    if result.returncode:
        raise StageError(f"terraform {args[0]} failed (exit {result.returncode})")
    return result.stdout or ""


def run_hook(stage: str, environ: dict | None = None) -> int:
    environ = dict(os.environ if environ is None else environ)
    env_dir = Path(environ.get("GENIERAILS_ENV_DIR") or "")
    if not env_dir.is_dir():
        raise StageError("GENIERAILS_ENV_DIR must name the env directory")
    env_name = environ.get("GENIERAILS_ENV_NAME") or env_dir.name
    layer_dir = env_dir / "data_access"
    desired = desired_generation(env_dir)
    expected = environ.get("GENIERAILS_POLICY_FINGERPRINT", "")
    if expected != desired["fingerprint"]:
        raise StageError(
            f"generated config is generation {desired['fingerprint']}, but the controller is "
            f"applying {expected or '<unset>'}; regenerate or restart the release"
        )
    old = load_snapshot(env_dir, environ.get("GENIERAILS_OLD_POLICY_FINGERPRINT", ""))
    retire_fp = environ.get("GENIERAILS_RETIRE_FINGERPRINT", "")
    retire = load_snapshot(env_dir, retire_fp) if retire_fp not in {"", desired["fingerprint"]} else None
    save_snapshot(env_dir, desired)
    rendered = render_stage(stage, desired, old, retire)
    write_stage(layer_dir, rendered)
    keep = {p["name"] for g in (desired, old) if g for p in g["fgac_policies"]}
    allowed = {p["name"] for p in (retire or {}).get("fgac_policies") or []} - keep
    with tempfile.TemporaryDirectory() as tmp:
        plan_file = Path(tmp) / f"{stage}.tfplan"
        _runner(env_name, layer_dir, "plan", "-input=false", f"-out={plan_file}")
        shown = _runner(env_name, layer_dir, "show", "-json", str(plan_file), capture=True)
        plan = json.loads(next(line for line in reversed(shown.splitlines()) if line.startswith("{")))
        problems = plan_violations(plan, stage, allowed)
        if problems:
            print(f"ERROR: the {stage} plan destroys protected resources; nothing was applied:",
                  file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        _runner(env_name, layer_dir, "apply-plan", str(plan_file))
    print(f"Policy stage {stage}: applied generation {desired['fingerprint']}"
          + (f" (retained {old['fingerprint']})" if old else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    fp = sub.add_parser("fingerprint", help="print the generated config's generation fingerprint")
    fp.add_argument("--env-dir", type=Path, required=True)
    hook = sub.add_parser("hook", help="run one staged-apply hook (environment-driven)")
    hook.add_argument("stage", choices=STAGES)
    args = parser.parse_args(argv)
    try:
        if args.command == "fingerprint":
            print(desired_generation(args.env_dir)["fingerprint"])
            return 0
        return run_hook(args.stage)
    except (StageError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
