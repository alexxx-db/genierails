#!/usr/bin/env python3
"""Maintain local Terraform state files with owner-only access."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

STATE_MODE = 0o600


def tighten_state_files(state_dir: Path) -> None:
    if not state_dir.exists():
        return
    paths = [*state_dir.glob("terraform.tfstate*"), state_dir / ".terraform" / "terraform.tfstate"]
    for path in sorted(paths):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"{path} is a symlink; refusing to change a Terraform state target through it")
        if not stat.S_ISREG(info.st_mode):
            continue
        mode = stat.S_IMODE(info.st_mode)
        if mode != STATE_MODE:
            print(
                f"WARNING: {path} had mode {mode:04o}; tightening to 0600 because Terraform state may hold secrets.",
                file=sys.stderr,
            )
            os.chmod(path, STATE_MODE)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(f"Usage: {Path(sys.argv[0]).name} <state-dir>", file=sys.stderr)
        return 2
    try:
        tighten_state_files(Path(args[0]))
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
