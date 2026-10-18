#!/usr/bin/env python3
"""Sync tag policy values from abac.auto.tfvars to Databricks via REST API.

The Databricks Terraform provider has a bug where it reorders tag policy
values after apply, causing "Provider produced inconsistent result" errors.
This script bypasses Terraform by updating tag policy values directly via
the Databricks REST API, so Terraform can use ignore_changes = [values] safely.

Modes:
    add-only  (default) only adds values. If a live value is no longer
              desired, nothing is written and the run fails, pointing at
              ``make apply-account``. Deterministic rehearse, release and
              apply use this: Terraform ignores tag-policy values, so an
              empty account plan doesn't prove a REST update is safe.
    replace   makes the live values equal the desired ones, removing the
              rest. Only account-layer applies use it: ``make apply-account``
              and the legacy account apply (unchanged behaviour).

In both modes a live deterministic gr_treatment value is kept: only the
staged retirement removes a generation.

Usage:
    python3 scripts/sync_tag_policies.py [--mode add-only|replace] [path/to/abac.auto.tfvars]
"""
import argparse
import json
import os
import sys
from pathlib import Path

WORK_DIR = Path.cwd()


def retained_deterministic_values(key: str, current_values: set[str]) -> set[str]:
    """Live deterministic gr_treatment values (``<treatment>__<type>__<gen>``)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from governance_policies import TAG_KEY, parse_treatment_value

    if key != TAG_KEY and not key.startswith(TAG_KEY + "_"):
        return set()
    return {value for value in current_values if parse_treatment_value(value)}


def _load_auth():
    """Read auth.auto.tfvars and return config dict + set SDK env vars."""
    auth_path = WORK_DIR / "auth.auto.tfvars"
    if not auth_path.exists():
        return {}
    try:
        import hcl2
    except ImportError:
        import subprocess
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", "python-hcl2>=7.3.1,<8"],
        )
        import hcl2

    with open(auth_path) as f:
        cfg = hcl2.load(f)

    mapping = {
        "databricks_workspace_host": "DATABRICKS_HOST",
        "databricks_client_id": "DATABRICKS_CLIENT_ID",
        "databricks_client_secret": "DATABRICKS_CLIENT_SECRET",
    }
    for tfvar_key, env_key in mapping.items():
        val = cfg.get(tfvar_key, "")
        if isinstance(val, list):
            val = val[0] if val else ""
        val = (val or "").strip()
        if val:
            os.environ[env_key] = val

    return cfg


def _str(v):
    return (v[0] if isinstance(v, list) else v or "").strip()


APPLY_ACCOUNT_HINT = (
    "Run 'make account-plan', get its manifest approved, then run 'make apply-account' "
    "in the approved operations stage to remove them."
)


def plan_updates(desired_policies, existing, mode):
    """``(updates, refused)``: per-key values to write, and removals add-only refuses.

    ``updates`` is ``[(actual_key, values, added, removed)]``; ``refused`` is
    ``[(actual_key, values that would be removed)]``.
    """
    updates, refused = [], []
    for tp in desired_policies:
        key = tp["key"]
        desired_values = set(tp["values"])
        # Exact match first, then suffix match (Terraform appends _<hex> suffix
        # to tag keys for account-level isolation in parallel test environments).
        current_values = existing.get(key)
        actual_key = key
        if current_values is None:
            for db_key, db_vals in existing.items():
                if db_key == key or db_key.startswith(key + "_"):
                    current_values = db_vals
                    actual_key = db_key
                    break

        if current_values is None:
            continue

        # A deterministic treatment value stays allowed while a retained policy
        # generation or a not-yet-retagged column may still use it; only the
        # staged retirement removes generations, so this sync never does.
        desired_values |= retained_deterministic_values(key, current_values)
        missing = desired_values - current_values
        removed = current_values - desired_values
        if mode == "add-only":
            if removed:
                refused.append((actual_key, sorted(removed)))
                continue
            if not missing:
                continue
        updates.append((actual_key, sorted(desired_values), sorted(missing), sorted(removed)))
    return updates, refused


def update_diff(updates):
    """Stable JSON shape for the values a replace sync would add/remove."""
    return [
        {"key": actual_key, "added": list(added), "removed": list(removed)}
        for actual_key, _values, added, removed in updates
        if added or removed
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sync tag policy values to Databricks.")
    parser.add_argument("--mode", choices=("add-only", "replace"), default="add-only")
    parser.add_argument("--dry-run", action="store_true", help="plan changes but do not write them")
    parser.add_argument("--json", action="store_true", help="with --dry-run, print the add/remove diff as JSON")
    parser.add_argument("tfvars", nargs="?", default=str(WORK_DIR / "abac.auto.tfvars"))
    args = parser.parse_args(argv)
    tfvars_path = Path(args.tfvars)
    if not tfvars_path.exists():
        if args.dry_run and args.json:
            print("[]")
        else:
            print(f"  [SKIP] {tfvars_path} not found")
        return

    import hcl2

    with open(tfvars_path) as f:
        config = hcl2.load(f)

    desired_policies = config.get("tag_policies", [])
    if not desired_policies:
        if args.dry_run and args.json:
            print("[]")
        else:
            print("  [SKIP] No tag_policies found in config")
        return

    auth_cfg = _load_auth()

    # Get auth token via SDK's config.authenticate()
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.tags import TagPolicy, Value
    host = _str(auth_cfg.get("databricks_workspace_host", ""))
    client_id = _str(auth_cfg.get("databricks_client_id", ""))
    client_secret = _str(auth_cfg.get("databricks_client_secret", ""))

    w = WorkspaceClient(
        host=host or None,
        client_id=client_id or None,
        client_secret=client_secret or None,
        product="genierails",
        product_version="0.1.0",
    )
    token = w.config.authenticate()  # {'Authorization': 'Bearer ...'}
    base = (host or os.environ.get("DATABRICKS_HOST", "")).rstrip("/")
    if not base:
        print("  [WARN] No workspace host found; skipping tag policy sync")
        return

    # List existing tag policies via SDK. The REST list path has returned 404
    # in some workspaces even though the tag policy service is available.
    existing = {}
    try:
        for tp in w.tag_policies.list_tag_policies():
            tag_key = getattr(tp, "tag_key", "") or ""
            values = {
                getattr(v, "name", "") or ""
                for v in (getattr(tp, "values", None) or [])
                if getattr(v, "name", "") or ""
            }
            existing[tag_key] = values
    except Exception as list_err:
        print(f"  [ERROR] Could not list tag policies ({list_err})")
        raise SystemExit(1)

    updates, refused = plan_updates(desired_policies, existing, args.mode)
    if args.dry_run:
        if args.json:
            print(json.dumps(update_diff(updates), sort_keys=True))
        else:
            for actual_key, _values, missing, removed in updates:
                changes = []
                if missing:
                    changes.append(f"would add {missing}")
                if removed:
                    changes.append(f"would remove {removed}")
                print(f"  [PLAN] {actual_key}: {', '.join(changes) if changes else 'already in sync'}")
        return

    if refused:
        # Nothing is written: a partial sync would leave the account half-updated.
        print("  [ERROR] Tag policy values are live but no longer desired; this sync only adds values:")
        for actual_key, values in refused:
            print(f"    - {actual_key}: would remove {values}")
        print(f"  {APPLY_ACCOUNT_HINT}")
        raise SystemExit(1)

    updated = 0
    for actual_key, values, missing, removed in updates:
        try:
            w.tag_policies.update_tag_policy(
                tag_key=actual_key,
                tag_policy=TagPolicy(
                    tag_key=actual_key,
                    values=[Value(name=v) for v in values],
                ),
                update_mask="values",
            )
            changes = []
            if missing:
                changes.append(f"added {missing}")
            if removed:
                changes.append(f"removed {removed}")
            if not changes:
                changes.append("reasserted desired values")
            print(f"  [SYNC] {actual_key}: {', '.join(changes)}")
            updated += 1
        except Exception as e:
            print(f"  [ERROR] {actual_key}: {e}")
            raise SystemExit(1)

    if updated:
        print(f"  Synced {updated} tag policy/ies")
    else:
        print("  Tag policies already in sync")


if __name__ == "__main__":
    main()
