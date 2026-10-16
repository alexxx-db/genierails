#!/usr/bin/env python3
"""The customer-repo .gitignore that make setup writes (design section 9).

GenieRails' own .gitignore ignores envs/, generated/ and masking_functions.sql
everywhere, so the upstream repo never tracks anyone's env. A customer repo
commits envs/ instead. This writes one managed block into <cloud>/.gitignore
that re-includes the committed env files and ignores every runtime file:
credentials, Terraform state and plans, locks, receipts, Genie ID and adoption
markers, live snapshots and, for deterministic envs, the layer files that
applying commands re-derive.

The rule (``--mode``, make setup's GITIGNORE):
  customer  write the block, or refresh it if present (first time: opt in)
  auto      the default: refresh the block only if <cloud>/.gitignore already
            has it, otherwise only print how to opt in. The upstream repo never
            has the block, so make setup never changes it there.
  off       do nothing
Only the lines between the markers are ever rewritten; every other line is
kept byte for byte. Re-running is a no-op; every change is printed as a diff.
The block lists each deterministic env by name, so re-run make setup after
adding an env or changing governance_mode.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

BEGIN = "# >>> GenieRails customer repo (make setup GITIGNORE=customer; edit outside this block) >>>"
END = "# <<< GenieRails customer repo <<<"

COMMON = """\
# Commit envs/: env.auto.tfvars, agents/, generated/, ack.txt, capture files.
!/envs/
!/envs/**/generated/
!/envs/**/masking_functions.sql
# Never commit credentials, Terraform state or plans, locks, receipts, Genie ID
# and adoption markers, or runtime records (all GenieRails runtime files under
# envs/ start with a dot: .genie_*, .policy_generations/, generated/.runtime/,
# generated/.live_refresh.json, generated/.governance.lock, .*.apply.sha, ...).
auth.auto.tfvars
*.tfstate
*.tfstate.backup
*.tfstate.*.backup
*.tfplan
.terraform/
__pycache__/
/envs/**/.*
/envs/**/classification.auto.tfvars
/envs/**/discovered_uc_tables.auto.tfvars
/envs/**/ddl/_fetched.sql
/envs/*/data_access/env.auto.tfvars
/.genierails-state/
"""


def _governance_mode(env_file: Path) -> str:
    try:
        import hcl2
        with open(env_file) as handle:
            mode = hcl2.load(handle).get("governance_mode", "legacy")
    except Exception:  # unreadable config: commit more, never hide a file
        return "legacy"
    return mode if mode in ("legacy", "deterministic") else "legacy"


def deterministic_envs(cloud_dir: Path, account_env: str) -> list[str]:
    envs = []
    for env_file in sorted((cloud_dir / "envs").glob("*/env.auto.tfvars")):
        name = env_file.parent.name
        if name != account_env and _governance_mode(env_file) == "deterministic":
            envs.append(name)
    return envs


def _workspace_envs(cloud_dir: Path, account_env: str) -> list[str]:
    return sorted(p.parent.name for p in (cloud_dir / "envs").glob("*/env.auto.tfvars")
                  if p.parent.name != account_env)


def block(cloud_dir: Path, account_env: str = "account") -> str:
    lines = [BEGIN, COMMON.rstrip("\n")]
    deterministic = deterministic_envs(cloud_dir, account_env)
    if deterministic:
        lines.append("# Deterministic envs: the layer files are runtime outputs, re-derived on apply.")
        for env in deterministic:
            lines += [f"/envs/{env}/abac.auto.tfvars", f"/envs/{env}/data_access/",
                      f"/envs/{env}/generated/genie_space_derived_acl_groups.auto.tfvars"]
        # The account layer file is a runtime output only when no legacy env commits it.
        if deterministic == _workspace_envs(cloud_dir, account_env):
            lines.append(f"/envs/{account_env}/abac.auto.tfvars")
    lines.append(END)
    return "\n".join(lines) + "\n"


def _split(text: str) -> tuple[str, str | None, str]:
    """(before, managed block or None, after); raises on damaged markers."""
    begins, ends = text.count(BEGIN), text.count(END)
    if begins == 0 and ends == 0:
        return text, None, ""
    start, stop = text.find(BEGIN), text.find(END)
    if begins != 1 or ends != 1 or stop < start:
        raise ValueError("its GenieRails block markers are damaged (expected one begin line, then one end line)")
    stop += len(END)
    if text[stop:stop + 1] == "\n":
        stop += 1
    return text[:start], text[start:stop], text[stop:]


def merged(text: str, managed: str) -> str:
    before, _current, after = _split(text)
    if _current is None:
        if before and not before.endswith("\n"):
            before += "\n"
        return before + ("\n" if before.strip() else "") + managed
    return before + managed + after


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cloud-dir", required=True, type=Path)
    parser.add_argument("--mode", default="auto")
    parser.add_argument("--account-env", default="account")
    parser.add_argument("--env", default="dev", help="for the opt-in hint only")
    args = parser.parse_args(argv)
    mode = args.mode or "auto"
    if mode not in ("customer", "auto", "off"):
        print(f"ERROR: GITIGNORE={mode}: use customer, auto (the default) or off.", file=sys.stderr)
        return 2
    if mode == "off":
        return 0
    path = args.cloud_dir / ".gitignore"
    rel = f"{args.cloud_dir.name}/.gitignore"
    text = path.read_text() if path.exists() else ""
    try:
        has_block = _split(text)[1] is not None
    except ValueError as exc:
        print(f"ERROR: not changing {path}: {exc}. Fix or remove the block, then re-run.", file=sys.stderr)
        return 1
    if mode == "auto" and not has_block:
        print(f"Version control: in your own repo, make setup ENV={args.env} GITIGNORE=customer "
              f"writes {rel} so envs/ is committed and runtime files are ignored.")
        return 0
    new = merged(text, block(args.cloud_dir, args.account_env))
    if new == text:
        print(f"{rel}: up to date (envs/ committed, runtime files ignored).")
        return 0
    sys.stdout.writelines(difflib.unified_diff(
        text.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{rel}", f"b/{rel}"))
    path.write_text(new)
    print(f"{rel}: {'updated' if has_block else 'wrote'} the GenieRails block above (lines outside it are unchanged).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
