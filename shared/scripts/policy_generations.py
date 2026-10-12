#!/usr/bin/env python3
"""Versioned policy generations and the staged-apply hooks for deterministic governance.

``scripts/staged_policy_apply.py`` (step 6) drives these hooks; this module
owns what each stage renders and what its plan may do. A **generation** is one
deterministic model: its column-mask policies (with principals), gr_treatment
tag values and functions, identified by ``governance_policies.model_fingerprint``.
Function, policy and treatment value names are content-versioned (see
governance_policies.py), so two generations never fight over one name.

Hooks are argv executables in ``scripts/policy_hooks/``, one per stage. Each
renders its stage into the data_access layer, checks the render (no tagged
column without a mask, no principal with two outside a tier move), plans it,
refuses any destroy the stage doesn't allow, and applies exactly that plan.
They read:

* ``GENIERAILS_ENV_DIR``: the env (``envs/<env>``); required.
* ``GENIERAILS_ENV_NAME``: defaults to the env dir's name.
* ``GENIERAILS_POLICY_FINGERPRINT``: must equal ``fingerprint`` of the
  generated config, so a hook never applies a model the controller didn't
  record (``abandon`` reads that generation from its snapshot instead).
* ``GENIERAILS_OLD_POLICY_FINGERPRINT``: the active generation, kept live.
* ``GENIERAILS_RETIRE_FINGERPRINT``: a generation an earlier release made
  obsolete; kept live until ``retire`` removes it.
* ``GENIERAILS_POLICY_KEEP_NEW_TAGS=1``: a switch was attempted, so newly
  governed columns may already carry their new value; keep it.
* ``GENIERAILS_COVERAGE_GATE_RUNNER``: when set, the coverage check re-runs on
  each render, so its result matches what the stage applies.

Stages (every one keeps all retained generations' functions):

* ``create``: replacement policies are added; retained policies keep their
  principals; every column keeps its old treatment value. A new policy on a
  value columns already carry (a new middle tier) waits for ``tighten``.
  Nothing is removed and nothing is retagged.
* ``tighten`` (tier moves only): every policy shared with the desired
  generation takes the union of its live and desired ``TO`` principals and the
  intersection of its ``EXCEPT`` principals, and the deferred policies are
  added. Coverage only grows: nobody masked before or after is unmasked, and
  only principals being moved can briefly hit ``MULTIPLE_MASKS``.
* ``switch``: columns are retagged to the desired values and policies take
  their desired principals. Policies of retained generations whose value no
  column carries any more stay until ``retire``; a retained policy on a value
  still in use that the desired generation drops leaves here (after tighten,
  the desired policies already cover everyone it covered).
* ``restore``: back to the ``create`` render (old values and principals) after
  a failed tighten or switch; newly governed columns keep their new value.
* ``retire``: the retire generation's policies leave.
* ``abandon``: a transition that never switched is dropped: back to the
  active generation, without the replacement's policies.
* ``drop-functions``: after ``retire``, drop the functions only the retired
  generation used, once no live policy calls them.
* ``grant``: last, once every phase above is applied and verified: re-apply
  the last stage's render with new SELECT allowed. Every earlier phase plans
  ``policy_stage`` values that withhold new grants (grants already applied
  stay), so access never opens before its protection is verified. Its plan
  may change no policy or treatment tag.

Snapshots of every generation a hook applies are kept next to the layer's
state, in ``<env>/data_access/.policy_generations/<fingerprint>.json``; the
last applied stage is recorded in ``stage.json`` there for verification.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SHARED = Path(__file__).resolve().parents[1]
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from governance_policies import (  # noqa: E402
    TAG_KEY, model_fingerprint, parse_treatment_value, replace_sql_library,
    sql_library_block,
)

APPLY_STAGES = ("create", "tighten", "switch", "restore", "retire", "abandon")
STAGES = APPLY_STAGES + ("drop-functions", "grant")
RUNNER = SHARED / "scripts" / "terraform_layer.sh"
COVERAGE_GATE = SHARED / "scripts" / "coverage_gate.py"
DEPLOY_FUNCTIONS = SHARED / "deploy_masking_functions.py"
LEDGER = Path(".policy_generations")
STAGE_RECORD = "stage.json"
ALL_USERS = "account users"
OUTSIDER = "<anyone outside the tiers>"
# Resources a staged apply may never destroy: a live policy, a treatment tag
# (a column would lose its mask), or the resource whose destroy drops functions.
PROTECTED_TYPES = {
    "databricks_policy_info", "databricks_entity_tag_assignment", "terraform_data.masking_functions_drop",
}
# Business access is only ever created once the new protection is switched in.
BUSINESS_GRANTS = {"catalog_access", "schema_access", "table_access"}
SETTINGS_KEYS = ("access_tier_groups", "raw_exempt_principals", "treatment_versions",
                 "column_overrides", "tier_access_overrides")
_VALUE_IN_CONDITION = re.compile(r"hasTagValue\(\s*'" + TAG_KEY + r"'\s*,\s*'([^']+)'\s*\)")


class StageError(RuntimeError):
    pass


def _load(path: Path) -> dict:
    import hcl2
    return hcl2.loads(path.read_text()) if path.is_file() else {}


def desired_generation(env_dir: Path) -> dict:
    """The generation the generated config describes, with the settings behind it."""
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
    env_cfg = _load(env_dir / "env.auto.tfvars")
    return {
        "fingerprint": model_fingerprint(cfg, functions),
        "fgac_policies": masks, "tags": tags, "functions_sql": functions,
        # What verification of this generation expects of each tier.
        "settings": {key: env_cfg[key] for key in SETTINGS_KEYS if key in env_cfg},
    }


def ledger_dir(env_dir: Path) -> Path:
    return env_dir / "data_access" / LEDGER


def write_json(path: Path, value: dict) -> Path:
    """Write ``value`` atomically: a crash leaves the old file or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return path


def save_snapshot(env_dir: Path, generation: dict) -> Path:
    return write_json(ledger_dir(env_dir) / f"{generation['fingerprint']}.json", generation)


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


def _statements(block: str) -> list[tuple[str, str, str]]:
    out, catalog, schema = [], "", ""
    for raw in (block or "").split(";\n"):
        stmt = raw.strip().rstrip(";")
        if stmt.upper().startswith("USE CATALOG "):
            catalog = stmt
        elif stmt.upper().startswith("USE SCHEMA "):
            schema = stmt
        elif stmt:
            out.append((catalog, schema, stmt))
    return out


def _join_statements(statements) -> str:
    out, current = [], None
    for catalog, schema, stmt in sorted(set(statements)):
        if (catalog, schema) != current:
            out.append(f"{catalog};\n{schema};")
            current = (catalog, schema)
        out.append(stmt + ";")
    return "\n\n".join(out) + ("\n" if out else "")


def _merge_functions(*blocks: str) -> str:
    """The union of several generations' function blocks, one statement each."""
    # CREATE OR REPLACE of identical text is idempotent.
    return _join_statements(s for block in blocks for s in _statements(block))


def function_name(stmt: str) -> str:
    match = re.match(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+([\w.`]+)", stmt, re.IGNORECASE)
    return match.group(1).replace("`", "").lower() if match else ""


def policy_value(policy: dict) -> str:
    """The treatment value a deterministic mask policy matches ("" if none)."""
    match = _VALUE_IN_CONDITION.search(str(policy.get("match_condition") or ""))
    return match.group(1) if match else ""


def _principals(policy: dict) -> tuple[list[str], list[str]]:
    return list(policy.get("to_principals") or []), list(policy.get("except_principals") or [])


def tightened(live: dict, wanted: dict) -> dict:
    """``live`` covering everyone either ``live`` or ``wanted`` covers."""
    live_to, live_except = _principals(live)
    wanted_to, wanted_except = _principals(wanted)
    keep = set(wanted_except)
    return dict(live) | {
        "to_principals": list(dict.fromkeys(live_to + wanted_to)),
        "except_principals": [p for p in live_except if p in keep],
    }


def _covers(policy: dict, principal: str) -> bool:
    to, excepted = _principals(policy)
    if principal == OUTSIDER:
        return ALL_USERS in to
    return (ALL_USERS in to or principal in to) and principal not in excepted


def masks_by_principal(policies: list[dict], value: str, universe: set[str]) -> dict[str, list[str]]:
    """Which mask policies on ``value`` apply to each principal (direct names; no nesting)."""
    on_value = [p for p in policies if policy_value(p) == value]
    return {who: sorted(p["name"] for p in on_value if _covers(p, who)) for who in universe}


def _universe(*generations) -> set[str]:
    names = {OUTSIDER}
    for generation in generations:
        for policy in (generation or {}).get("fgac_policies") or []:
            to, excepted = _principals(policy)
            names.update(to + excepted)
    names.discard(ALL_USERS)
    return names


def _tag_key(column: str) -> str:
    return f"columns|{column}|{TAG_KEY}"


def plan_stage(stage: str, desired: dict, old: dict | None, retire: dict | None,
               *, keep_new_tags: bool = False) -> dict:
    """What ``stage`` renders, what its plan may delete, and why it is unsafe (if it is)."""
    if stage not in APPLY_STAGES:
        raise StageError(f"unknown stage {stage!r}")
    old_tags = dict((old or {}).get("tags") or {})
    desired_tags = dict(desired["tags"])
    create_tags = dict(old_tags)
    if keep_new_tags:
        create_tags.update({c: v for c, v in desired_tags.items() if c not in old_tags})
    retained = [g for g in (old, retire) if g]
    desired_defs = {p["name"]: p for p in desired["fgac_policies"]}
    retained_defs: dict[str, dict] = {}
    for generation in reversed(retained):  # the active generation's definition wins
        retained_defs.update({p["name"]: p for p in generation["fgac_policies"]})
    retained_values = {policy_value(p) for p in retained_defs.values()}
    # A new policy on a value columns already carry changes who those columns
    # mask, so it is a tier move: it waits for tighten.
    deferred = {
        name for name, policy in desired_defs.items()
        if name not in retained_defs and policy_value(policy) in retained_values
        and policy_value(policy) in set(create_tags.values())
    }
    live = dict(desired_defs) | retained_defs
    allowed_policies: set[str] = set()
    allowed_tags: set[str] = set()

    if stage in ("create", "restore"):
        policies = {n: p for n, p in live.items() if n not in deferred}
        tags = create_tags
        if stage == "restore":
            allowed_policies = set(deferred)
            allowed_tags = {_tag_key(c) for c in desired_tags.keys() - create_tags.keys()}
    elif stage == "tighten":
        policies = dict(live)
        for name in desired_defs.keys() & retained_defs.keys():
            policies[name] = tightened(retained_defs[name], desired_defs[name])
        tags = create_tags
    elif stage == "abandon":
        policies = dict(retained_defs)
        tags = dict(old_tags)
        allowed_policies = desired_defs.keys() - retained_defs.keys()
    else:  # switch, retire
        kept_gens = [old] if stage == "retire" else retained
        in_use = set(desired_tags.values())
        policies = dict(desired_defs)
        for generation in kept_gens:
            for policy in (generation or {}).get("fgac_policies") or []:
                if policy["name"] not in policies and policy_value(policy) not in in_use:
                    policies[policy["name"]] = policy
        tags = desired_tags
        allowed_policies = set(retained_defs) - set(policies)
        if stage == "switch":
            # Only policies on values still in use leave here; obsolete ones wait for retire.
            allowed_policies = {n for n in allowed_policies if policy_value(retained_defs[n]) in in_use}
        allowed_tags = {_tag_key(c) for c in (old_tags.keys() | create_tags.keys()) - desired_tags.keys()}

    functions = _merge_functions(*(g["functions_sql"] for g in [desired, old, retire] if g))
    rendered = {"fgac_policies": [policies[name] for name in sorted(policies)], "tags": tags,
                "functions_sql": functions}
    return {
        "rendered": rendered,
        "allowed_policy_deletes": sorted(allowed_policies),
        "allowed_tag_deletes": sorted(allowed_tags),
        "problems": stage_problems(stage, rendered, desired, old),
    }


def render_stage(stage: str, desired: dict, old: dict | None, retire: dict | None,
                 *, keep_new_tags: bool = False) -> dict:
    """Policies, tags and functions the data_access layer holds for ``stage``."""
    return plan_stage(stage, desired, old, retire, keep_new_tags=keep_new_tags)["rendered"]


def has_tier_move(desired: dict, old: dict | None, retire: dict | None = None,
                  *, keep_new_tags: bool = False) -> bool:
    """Whether the change moves principals on a value columns keep (needs tighten)."""
    create = render_stage("create", desired, old, retire, keep_new_tags=keep_new_tags)
    tighten = render_stage("tighten", desired, old, retire, keep_new_tags=keep_new_tags)
    return create["fgac_policies"] != tighten["fgac_policies"]


def stage_problems(stage: str, rendered: dict, desired: dict, old: dict | None) -> list[str]:
    """Why a render would leave a governed column unmasked or doubly masked.

    Pure, on direct principal names (the disjointness test of the policy
    builder covers nested groups). Every tagged column's value must have a
    policy; every principal gets at most one mask, except, while tightening,
    principals whose mask differs between the old and desired generation;
    and tightening never uncovers anyone the old or desired generation masks.
    """
    problems = []
    policies = rendered["fgac_policies"]
    universe = _universe(rendered, desired, old)
    old_policies = (old or {}).get("fgac_policies") or []
    for column, value in sorted(rendered["tags"].items()):
        if not any(policy_value(p) == value for p in policies):
            problems.append(f"{column} would carry {value}, which no policy masks")
    for value in sorted(set(rendered["tags"].values())):
        now = masks_by_principal(policies, value, universe)
        before = masks_by_principal(old_policies, value, universe)
        after = masks_by_principal(desired["fgac_policies"], value, universe)
        for who in sorted(universe):
            mover = bool(old) and before[who] != after[who]
            if len(now[who]) > 1 and not (stage == "tighten" and mover):
                problems.append(f"{who} would get {len(now[who])} masks on {value} ({', '.join(now[who])})")
            if stage == "tighten" and not now[who] and (before[who] or after[who]):
                problems.append(f"tightening would unmask {value} for {who}")
    return problems


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


def plan_violations(plan: dict, stage: str, allowed_policy_deletes=frozenset(),
                    allowed_tag_deletes=frozenset()) -> list[str]:
    """Changes in a plan that the stage may not make.

    No stage replaces a policy, a treatment tag or the function drop. Deletes
    are allowed only for the policies and treatment tags the stage itself
    removes (see plan_stage), and business grants are created only once the
    new protection is switched in.
    """
    problems = []
    for change in plan.get("resource_changes") or []:
        actions = (change.get("change") or {}).get("actions") or []
        address = str(change.get("address") or "")
        rtype = str(change.get("type") or "")
        key = str(change.get("index") or "")
        if ("create" in actions and rtype == "databricks_grant" and change.get("name") in BUSINESS_GRANTS
                and stage in ("create", "tighten", "restore", "abandon")):
            problems.append(f"{address}: business access is only granted after the switch")
        if stage == "grant" and rtype in ("databricks_policy_info", "databricks_entity_tag_assignment") \
                and set(actions) - {"no-op", "read"}:
            problems.append(f"{address}: {'/'.join(actions)} (the grant phase changes access only)")
            continue
        if "delete" not in actions:
            continue
        protected = rtype in PROTECTED_TYPES or any(address.endswith(t) for t in PROTECTED_TYPES if "." in t)
        if not protected:
            continue
        pure_delete = "create" not in actions
        if pure_delete and rtype == "databricks_policy_info" and key in allowed_policy_deletes:
            continue
        if pure_delete and rtype == "databricks_entity_tag_assignment" and key in allowed_tag_deletes:
            continue
        problems.append(f"{address}: {'/'.join(actions)}")
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


def _rerun_coverage_gate(env_dir: Path, env_name: str, runner: str) -> None:
    """Re-judge the coverage check on the render, so its result matches what is applied."""
    result = subprocess.run(
        [sys.executable, str(COVERAGE_GATE), "run", "--env-dir", str(env_dir),
         "--env-name", env_name, "--runner", runner], check=False,
    )
    if result.returncode:
        raise StageError(f"the coverage check stopped the stage (exit {result.returncode})")


def stage_record_path(env_dir: Path) -> Path:
    return ledger_dir(env_dir) / STAGE_RECORD


def load_stage_record(env_dir: Path) -> dict | None:
    path = stage_record_path(env_dir)
    return json.loads(path.read_text()) if path.is_file() else None


def run_hook(stage: str, environ: dict | None = None) -> int:
    environ = dict(os.environ if environ is None else environ)
    env_dir = Path(environ.get("GENIERAILS_ENV_DIR") or "")
    if not env_dir.is_dir():
        raise StageError("GENIERAILS_ENV_DIR must name the env directory")
    if stage == "drop-functions":
        return drop_functions(env_dir, environ)
    if stage == "grant":
        return grant(env_dir, environ)
    env_name = environ.get("GENIERAILS_ENV_NAME") or env_dir.name
    layer_dir = env_dir / "data_access"
    expected = environ.get("GENIERAILS_POLICY_FINGERPRINT", "")
    if stage == "abandon":
        if not expected:
            raise StageError("abandon needs GENIERAILS_POLICY_FINGERPRINT (the transition to drop)")
        desired = load_snapshot(env_dir, expected)
    else:
        desired = desired_generation(env_dir)
        if expected != desired["fingerprint"]:
            raise StageError(
                f"generated config is generation {desired['fingerprint']}, but the controller is "
                f"applying {expected or '<unset>'}; regenerate or restart the release"
            )
    old_fp = environ.get("GENIERAILS_OLD_POLICY_FINGERPRINT", "")
    old = load_snapshot(env_dir, old_fp)
    retire_fp = environ.get("GENIERAILS_RETIRE_FINGERPRINT", "")
    retire = load_snapshot(env_dir, retire_fp) if retire_fp not in {"", desired["fingerprint"], old_fp} else None
    if stage != "abandon":
        save_snapshot(env_dir, desired)
    keep_new_tags = environ.get("GENIERAILS_POLICY_KEEP_NEW_TAGS") == "1"
    staged = plan_stage(stage, desired, old, retire, keep_new_tags=keep_new_tags)
    if staged["problems"]:
        print(f"ERROR: the {stage} render would weaken a governed column; nothing was applied:",
              file=sys.stderr)
        for problem in staged["problems"]:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    write_stage(layer_dir, staged["rendered"])
    if environ.get("GENIERAILS_COVERAGE_GATE_RUNNER"):
        _rerun_coverage_gate(env_dir, env_name, environ["GENIERAILS_COVERAGE_GATE_RUNNER"])
    with tempfile.TemporaryDirectory() as tmp:
        plan_file = Path(tmp) / f"{stage}.tfplan"
        _runner(env_name, layer_dir, "plan", "-input=false", f"-var=policy_stage={stage}", f"-out={plan_file}")
        shown = _runner(env_name, layer_dir, "show", "-json", str(plan_file), capture=True)
        plan = json.loads(next(line for line in reversed(shown.splitlines()) if line.startswith("{")))
        problems = plan_violations(plan, stage, set(staged["allowed_policy_deletes"]),
                                   set(staged["allowed_tag_deletes"]))
        if problems:
            print(f"ERROR: the {stage} plan destroys protected resources; nothing was applied:",
                  file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        _runner(env_name, layer_dir, "apply-plan", str(plan_file))
    write_json(stage_record_path(env_dir), {
        "stage": stage, "desired": desired["fingerprint"], "old": old_fp,
        "retire": retire["fingerprint"] if retire else "", "keep_new_tags": keep_new_tags,
        "rendered": staged["rendered"],
        "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    print(f"Policy stage {stage}: applied generation {desired['fingerprint']}"
          + (f" (retained {old['fingerprint']})" if old else ""))
    return 0


def grant(env_dir: Path, environ: dict) -> int:
    """Re-apply the last verified render with new SELECT allowed; nothing else may change."""
    env_name = environ.get("GENIERAILS_ENV_NAME") or env_dir.name
    layer_dir = env_dir / "data_access"
    expected = environ.get("GENIERAILS_POLICY_FINGERPRINT", "")
    record = load_stage_record(env_dir)
    if not record or record.get("desired") != expected or desired_generation(env_dir)["fingerprint"] != expected:
        raise StageError("grant: the last applied stage is not of the generation the controller is applying")
    write_stage(layer_dir, record["rendered"])
    if environ.get("GENIERAILS_COVERAGE_GATE_RUNNER"):
        _rerun_coverage_gate(env_dir, env_name, environ["GENIERAILS_COVERAGE_GATE_RUNNER"])
    with tempfile.TemporaryDirectory() as tmp:
        plan_file = Path(tmp) / "grant.tfplan"
        _runner(env_name, layer_dir, "plan", "-input=false", "-var=policy_stage=grant", f"-out={plan_file}")
        shown = _runner(env_name, layer_dir, "show", "-json", str(plan_file), capture=True)
        plan = json.loads(next(line for line in reversed(shown.splitlines()) if line.startswith("{")))
        problems = plan_violations(plan, "grant")
        if problems:
            print("ERROR: the grant plan changes protection; nothing was applied:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        _runner(env_name, layer_dir, "apply-plan", str(plan_file))
    print(f"Policy stage grant: access applied on generation {expected}")
    return 0


def functions_to_drop(desired: dict, old: dict | None, retire: dict) -> str:
    """SQL (with its USE context) of the functions only ``retire`` defines."""
    kept = {function_name(stmt) for g in (desired, old) if g for _, _, stmt in _statements(g["functions_sql"])}
    return _join_statements(s for s in _statements(retire["functions_sql"]) if function_name(s[2]) not in kept)


def _live_function_users(env_dir: Path, catalogs: set[str]) -> set[str]:
    """Function names (lowercase, unqualified) live column-mask policies on ``catalogs`` call."""
    from scripts.audit_schema_drift import _get_sdk_client

    w = _get_sdk_client(env_dir / "data_access")
    used = set()
    for catalog in sorted(catalogs):
        for policy in w.policies.list_policies("CATALOG", catalog):
            mask = getattr(policy, "column_mask", None)
            name = str(getattr(mask, "function_name", "") or "") if mask else ""
            if name:
                used.add(name.rsplit(".", 1)[-1].lower())
    return used


def _resolve_warehouse(env_dir: Path, environ: dict) -> str:
    """The warehouse verify-access uses (explicit, configured, or the applied one)."""
    result = subprocess.run(
        [sys.executable, str(SHARED / "scripts" / "resolve_env_config.py"), "warehouse", "--env-dir", str(env_dir),
         "--terraform-runner", str(RUNNER), "--env-name", environ.get("GENIERAILS_ENV_NAME") or env_dir.name],
        text=True, capture_output=True, check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise StageError("drop-functions: no SQL warehouse resolved; set WAREHOUSE_ID or sql_warehouse_id")
    return result.stdout.strip().splitlines()[-1]


def drop_functions(env_dir: Path, environ: dict, *, live_users=None, run=subprocess.run) -> int:
    """Drop the retired generation's functions once no live policy calls them."""
    desired = desired_generation(env_dir)
    if environ.get("GENIERAILS_POLICY_FINGERPRINT", "") != desired["fingerprint"]:
        raise StageError("drop-functions: the generated config changed since the controller started")
    retire = load_snapshot(env_dir, environ.get("GENIERAILS_RETIRE_FINGERPRINT", ""))
    if retire is None:
        print("Policy stage drop-functions: no retired generation; nothing to drop")
        return 0
    old = load_snapshot(env_dir, environ.get("GENIERAILS_OLD_POLICY_FINGERPRINT", ""))
    sql = functions_to_drop(desired, old, retire)
    names = {function_name(stmt).rsplit(".", 1)[-1] for _, _, stmt in _statements(sql)}
    if not names:
        print("Policy stage drop-functions: every retired function is still used; nothing to drop")
        return 0
    catalogs = {p["catalog"] for g in (desired, old, retire) if g for p in g["fgac_policies"]}
    in_use = names & (live_users or _live_function_users)(env_dir, catalogs)
    if in_use:
        print("ERROR: live policies still call " + ", ".join(sorted(in_use)) + "; nothing was dropped",
              file=sys.stderr)
        return 1
    layer = env_dir / "data_access"
    auth = _load(layer / "auth.auto.tfvars")
    warehouse = environ.get("GENIERAILS_WAREHOUSE_ID", "") or _resolve_warehouse(env_dir, environ)
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as handle:
        handle.write(sql)
        sql_file = handle.name
    try:
        result = run([sys.executable, str(DEPLOY_FUNCTIONS), "--sql-file", sql_file, "--warehouse-id", warehouse,
                      "--auth-file", str(layer / "auth.auto.tfvars"),
                      "--host", str(auth.get("databricks_workspace_host") or ""), "--drop"], check=False)
    finally:
        os.unlink(sql_file)
    if result.returncode:
        print(f"ERROR: dropping retired functions failed (exit {result.returncode})", file=sys.stderr)
        return 1
    print(f"Policy stage drop-functions: dropped {len(names)} function(s) of generation {retire['fingerprint']}")
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
