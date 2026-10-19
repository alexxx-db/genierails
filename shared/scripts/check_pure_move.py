#!/usr/bin/env python3
"""Check that code split out of generate_abac.py moved without changing (18.7).

The baseline records the sha256 of every top-level definition (function,
class or module-level assignment) in generate_abac.py at the commit before the
split. A definition no longer in generate_abac.py must:

  - be defined in exactly one split module, byte-identical to the baseline;
  - be re-exported by generate_abac.py (`from <module> import <name>`), so
    `import generate_abac` and `from generate_abac import X` keep working.

A split module may hold only moved definitions: new code there is not a move.

Usage:
    check_pure_move.py                     # check the working tree
    check_pure_move.py --ref <commit>      # also prove the baseline is <commit>'s
    check_pure_move.py --write-baseline --ref <commit>
"""

import argparse
import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

SHARED = Path(__file__).resolve().parents[1]
SOURCE = "generate_abac.py"
# Modules that code split out of generate_abac.py lives in, beside it in shared/.
SPLIT_MODULES = ("hcl_text",)
BASELINE = SHARED / "tests" / "fixtures" / "generate_abac_split_baseline.json"


def definitions(text: str) -> dict[str, list[str]]:
    """Top-level name -> the exact source of each of its definitions."""
    lines = text.splitlines(keepends=True)
    found: dict[str, list[str]] = {}
    for node in ast.parse(text).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            start = node.lineno
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
            start = node.lineno
        else:
            continue
        segment = "".join(lines[start - 1:node.end_lineno])
        for name in names:
            found.setdefault(name, []).append(segment)
    return found


def reexports(text: str) -> dict[str, str]:
    """Names generate_abac.py imports at top level from a split module -> module."""
    names = {}
    for node in ast.parse(text).body:
        if isinstance(node, ast.ImportFrom) and node.module in SPLIT_MODULES and node.level == 0:
            for alias in node.names:
                if alias.asname in (None, alias.name):
                    names[alias.name] = node.module
    return names


def _sha(segment: str) -> str:
    return hashlib.sha256(segment.encode()).hexdigest()


def baseline_from_text(text: str, ref: str) -> dict:
    defs = {name: [_sha(s) for s in segments] for name, segments in sorted(definitions(text).items())}
    return {"source": f"shared/{SOURCE}", "base_commit": ref, "definitions": defs}


def git_source(ref: str) -> str:
    return subprocess.run(
        ["git", "-C", str(SHARED), "show", f"{ref}:shared/{SOURCE}"],
        check=True, capture_output=True, text=True,
    ).stdout


def check(root: Path, baseline: dict) -> tuple[list[str], list[str]]:
    """Return (moved names, problems) for the tree under root (a shared/ dir)."""
    source = (root / SOURCE).read_text()
    current = definitions(source)
    exported = reexports(source)
    modules = {m: definitions((root / f"{m}.py").read_text())
               for m in SPLIT_MODULES if (root / f"{m}.py").exists()}
    base = baseline["definitions"]
    moved, problems = [], []

    for name, hashes in base.items():
        homes = [m for m, defs in modules.items() if name in defs]
        if name in current:
            if homes:
                problems.append(f"{name}: defined in {SOURCE} and in {', '.join(homes)}")
            continue
        if len(homes) != 1:
            problems.append(f"{name}: gone from {SOURCE} but defined in "
                            f"{len(homes)} split modules ({', '.join(homes) or 'none'})")
            continue
        module = homes[0]
        got = [_sha(s) for s in modules[module][name]]
        if got != hashes:
            problems.append(f"{name}: {module}.py body differs from {baseline['base_commit']}")
        if exported.get(name) != module:
            problems.append(f"{name}: {SOURCE} does not re-export it from {module}")
        moved.append(name)

    for module, defs in modules.items():
        for name in defs:
            if name not in base:
                problems.append(f"{name}: new definition in {module}.py (not a move)")
    for name, module in exported.items():
        if name not in modules.get(module, {}):
            problems.append(f"{name}: re-exported from {module} but not defined there")
    return moved, problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ref", help="base commit; check the baseline against it")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--root", type=Path, default=SHARED, help="shared/ dir to check")
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    args = parser.parse_args(argv)

    if args.write_baseline:
        if not args.ref:
            parser.error("--write-baseline needs --ref")
        sha = subprocess.run(["git", "-C", str(SHARED), "rev-parse", args.ref],
                             check=True, capture_output=True, text=True).stdout.strip()
        args.baseline.write_text(json.dumps(baseline_from_text(git_source(sha), sha), indent=1) + "\n")
        print(f"Wrote {args.baseline} from {sha}")
        return 0

    baseline = json.loads(args.baseline.read_text())
    if args.ref:
        fresh = baseline_from_text(git_source(args.ref), baseline["base_commit"])
        if fresh["definitions"] != baseline["definitions"]:
            print(f"FAIL: {args.baseline.name} does not match {args.ref}:shared/{SOURCE}")
            return 1
    moved, problems = check(args.root, baseline)
    for problem in problems:
        print(f"FAIL: {problem}")
    if problems:
        return 1
    print(f"OK: {len(moved)} definition(s) moved byte-identical from "
          f"{baseline['base_commit'][:12]} and re-exported by {SOURCE}")
    for name in moved:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
