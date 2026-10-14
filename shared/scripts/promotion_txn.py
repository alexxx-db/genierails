#!/usr/bin/env python3
"""All-or-nothing promotion: snapshot the trees a promotion writes, restore them on failure.

make promote (cross-env) and make promote-to write the destination env and
the shared account layer in several steps. When any step refuses, restore
puts every file, symlink and directory under those trees back byte for byte
and removes what the run added, so a refused promotion leaves nothing
changed. ``.terraform`` directories (providers, never written by a promotion)
are neither copied nor touched.

  promotion_txn.py snapshot --to <tmp> <dir> [<dir> ...]
  promotion_txn.py restore --from <tmp>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

SKIP = frozenset({".terraform"})
MANIFEST = "manifest.json"


def _walk(root: Path):
    """Every path under root (not root itself), parents before children, skipping SKIP dirs."""
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        base = Path(current)
        for name in dirs:
            yield base / name
        for name in sorted(files):
            yield base / name


def snapshot(dirs: list[Path], to: Path) -> None:
    to.mkdir(parents=True, exist_ok=True)
    entries = []
    for index, root in enumerate(dirs):
        root = root.absolute()
        copy = to / str(index)
        entries.append({"root": str(root), "copy": str(copy), "existed": root.is_dir()})
        if not root.is_dir():
            continue
        copy.mkdir()
        shutil.copystat(root, copy)
        for path in _walk(root):
            target = copy / path.relative_to(root)
            if path.is_symlink():
                os.symlink(os.readlink(path), target)
            elif path.is_dir():
                target.mkdir()
                shutil.copystat(path, target)
            else:
                shutil.copy2(path, target)
    (to / MANIFEST).write_text(json.dumps(entries))


def _remove(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def restore(source: Path) -> None:
    for entry in json.loads((source / MANIFEST).read_text()):
        root, copy = Path(entry["root"]), Path(entry["copy"])
        if not entry["existed"]:
            if root.is_dir():
                for child in root.iterdir():
                    if child.name not in SKIP:
                        _remove(child)
                if not any(root.iterdir()):
                    root.rmdir()
            continue
        current = set(_walk(root)) if root.is_dir() else set()
        wanted = {root / p.relative_to(copy) for p in _walk(copy)}
        for path in sorted(current - wanted, key=lambda p: len(p.parts), reverse=True):
            if path.exists() or path.is_symlink():
                _remove(path)
        root.mkdir(parents=True, exist_ok=True)
        for saved in _walk(copy):
            path = root / saved.relative_to(copy)
            if saved.is_symlink():
                if not (path.is_symlink() and os.readlink(path) == os.readlink(saved)):
                    if path.exists() or path.is_symlink():
                        _remove(path)
                    os.symlink(os.readlink(saved), path)
            elif saved.is_dir():
                if path.is_symlink() or (path.exists() and not path.is_dir()):
                    _remove(path)
                path.mkdir(exist_ok=True)
                shutil.copystat(saved, path)
            else:
                if path.is_symlink() or path.is_dir():
                    _remove(path)
                if not path.is_file() or path.read_bytes() != saved.read_bytes():
                    shutil.copy2(saved, path)
                else:
                    shutil.copystat(saved, path)
        shutil.copystat(copy, root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot")
    snap.add_argument("--to", required=True, type=Path)
    snap.add_argument("dirs", nargs="+", type=Path)
    rest = sub.add_parser("restore")
    rest.add_argument("--from", dest="source", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            snapshot(args.dirs, args.to)
        else:
            restore(args.source)
    except OSError as exc:
        print(f"ERROR: promotion {args.command} failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
