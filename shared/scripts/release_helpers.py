#!/usr/bin/env python3
"""Small helpers for the unified production release."""

import argparse
import sys
from pathlib import Path

import hcl2


def clear_old_receipts(env_dir: Path) -> None:
    for name in (".certified.json", ".certified.pending.json"):
        (env_dir / "generated" / name).unlink(missing_ok=True)


def column_mask_policies(env_dir: Path) -> list[str]:
    """Column-mask policy names in the env's generated and promoted config."""
    from scripts.runtime_overlay import effective

    names = []
    for path in (effective(env_dir, "abac.auto.tfvars"), env_dir / "data_access" / "abac.auto.tfvars"):
        if path.is_file():
            names += [str(policy.get("name", "?")) for policy in hcl2.load(path.open()).get("fgac_policies") or []
                      if policy.get("policy_type") == "POLICY_TYPE_COLUMN_MASK"]
    return sorted(set(names))


SHARED = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SHARED))  # verify_effective_access: the spec verify-access runs


def _refuse(env: str, reason: str) -> int:
    print(f"release: {reason}; nothing was applied.\n"
          f"  Fix the masking policies or the account groups (dev, then rehearse and re-promote),\n"
          f"  or pass a VERIFY_SPEC=<file> whose column_masks check every masked column.",
          file=sys.stderr)
    return 1


def _promoted_tfvars(env_dir: Path) -> Path:
    return env_dir / "data_access" / "abac.auto.tfvars"


def require_mask_proof(env_dir: Path, env: str, key: str, spec: str,
                       account_tfvars: Path | None = None, tfvars: Path | None = None) -> int:
    """Refuse a release whose verify-access could not prove every mask.

    The masked columns to prove are every column a column-mask policy's tags
    match (required_mask_columns), whatever principals a check could use.
    verify-access derives a check per masked column, but drops a mask with no
    concrete masked tier (e.g. "account users" without account groups); an
    explicit VERIFY_SPEC replaces the derived checks altogether. Each must
    cover every required column. Row-pairing keys need no setting here:
    verify-access picks one per table (or uses verify_key_columns /
    VERIFY_KEY_COLUMN), and release proves them all as the admin
    (verify-access --check-keys-only) before it applies. Run before the
    release lock and again after release promotes the live-derived config
    (prod's tag assignments only exist then), before it applies.
    """
    from verify_effective_access import (
        load_key_map, load_spec_from_file, load_spec_from_tfvars, required_mask_columns_from_tfvars,
        unchecked_mask_columns, weaker_explicit_spec_checks,
    )

    masks = column_mask_policies(env_dir)
    if not masks and not spec.strip():
        return 0
    tfvars = tfvars or _promoted_tfvars(env_dir)
    try:
        required = required_mask_columns_from_tfvars(tfvars) if tfvars.is_file() else set()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _refuse(env, f"could not read the masked columns from {tfvars} ({exc})")

    if not spec.strip():
        if not required:
            return 0  # nothing tagged yet; re-checked once release has derived the assignments
        # The checks verify-access will derive, with the account groups it uses.
        if account_tfvars is not None and not account_tfvars.is_file():
            return _refuse(env, f"the account config {account_tfvars} that verify-access reads groups "
                                f"from is missing, so it could not check {len(required)} masked column(s)")
        try:
            checks = load_spec_from_tfvars(
                tfvars, account_tfvars, key_column=key.strip(),
                key_column_by_table=load_key_map(env_dir / "env.auto.tfvars"),
            ).column_masks
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return _refuse(env, f"verify-access could not derive its checks from {tfvars} ({exc})")
        unchecked = unchecked_mask_columns(required, checks, keyed_only=False)
        if unchecked:
            return _refuse(env, f"verify-access would derive no check for masked column(s) "
                                f"{', '.join(unchecked)}: their policies have no concrete masked group "
                                "to test (e.g. 'account users' with no groups in the account config)")
        return 0

    # verify-access reads VERIFY_SPEC from shared/; resolve it the same way.
    path = Path(spec.strip())
    path = path if path.is_absolute() else SHARED / path
    try:
        explicit = load_spec_from_file(path)
        checks = explicit.column_masks
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return _refuse(env, f"VERIFY_SPEC {path} could not be read ({exc})")
    if masks and not checks:
        return _refuse(env, f"VERIFY_SPEC {path} has no column-mask checks, so it proves none of "
                            f"the {len(masks)} column mask(s)")
    # A keyless check is keyed live, like a derived one (and proven before apply).
    unchecked = unchecked_mask_columns(required, checks, keyed_only=False)
    if unchecked:
        return _refuse(env, f"VERIFY_SPEC {path} does not check masked column(s): {', '.join(unchecked)}")
    if tfvars.is_file():
        if account_tfvars is not None and not account_tfvars.is_file():
            return _refuse(env, f"the account config {account_tfvars} that verify-access derives groups "
                                "from is missing, so VERIFY_SPEC strength could not be checked")
        try:
            derived = load_spec_from_tfvars(
                tfvars, account_tfvars, key_column=key.strip(),
                key_column_by_table=load_key_map(env_dir / "env.auto.tfvars"),
                env_file=env_dir / "env.auto.tfvars",
            )
            derived.column_masks = [
                check for check in derived.column_masks
                if (check.table.lower(), check.column.lower()) in required
            ]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return _refuse(env, f"verify-access could not derive the minimum checks from {tfvars} ({exc})")
        weaker = weaker_explicit_spec_checks(derived, explicit)
        if weaker:
            return _refuse(env, f"VERIFY_SPEC {path} is weaker than the policy-derived gate spec; "
                           f"missing or weakened check(s): {'; '.join(weaker)}")
    return 0


def access_summary(env_dir: Path) -> list[str]:
    """What a deterministic release granted, by mode; never claims access nothing grants."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from table_readers import _load_hcl, agents, normalize

    cfg = _load_hcl(env_dir / "env.auto.tfvars")
    lines = []
    if normalize(cfg.get("table_readers")):
        lines.append("  Table access:  SELECT from table_readers (new grants only after the coverage check passed)")
    else:
        lines.append("  Table access:  none granted (no table_readers); masks apply to whoever already has access")
    runnable = [agent["name"] for agent in agents(env_dir, cfg) if agent["acl_groups"]]
    if runnable:
        lines.append("  Agent access:  CAN_RUN from acl_groups, opened only where the live agent coverage check "
                     f"passed ({', '.join(runnable)})")
    else:
        lines.append("  Agent access:  none (no agent with acl_groups)")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("clear-old-receipts", "failed", "require-mask-proof", "access-summary"))
    parser.add_argument("env_dir", type=Path)
    parser.add_argument("--env", default="prod")
    parser.add_argument("--reason", default="the release failed")
    parser.add_argument("--key", default="", help="Resolved verify key column (explicit or saved).")
    parser.add_argument("--spec", default="", help="VERIFY_SPEC, if given.")
    parser.add_argument("--account-tfvars", type=Path, default=None,
                        help="Account abac.auto.tfvars (group names), as verify-access uses.")
    parser.add_argument("--tfvars", type=Path, default=None,
                        help="Policy tfvars to derive the gate minimum from (default: promoted config).")
    args = parser.parse_args()
    if args.command == "clear-old-receipts":
        clear_old_receipts(args.env_dir)
        return 0
    if args.command == "require-mask-proof":
        return require_mask_proof(args.env_dir, args.env, args.key, args.spec,
                                  args.account_tfvars, args.tfvars)
    if args.command == "access-summary":
        print("\n".join(access_summary(args.env_dir)))
        return 0
    env_file = args.env_dir / "env.auto.tfvars"
    print(f"release: {args.reason}.\n  Business access (table SELECT / Genie CAN_RUN) for {args.env} may be PARTLY APPLIED;\n"
          f"  Terraform granted only what passed the coverage check. To withdraw access, remove the groups\n"
          f"  (or set acl_groups = []) in {env_file}, then run: make apply ENV={args.env}\n"
          f"  Then fix the cause and re-run make release ENV={args.env}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
