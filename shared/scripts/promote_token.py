#!/usr/bin/env python3
"""One-time proof that cross-env promote was called by promote-to.

The public make promote SOURCE_ENV=... DEST_ENV=... form is legacy-compatible,
but deterministic destinations must go through make promote-to so the wrapper
also promotes agents, custom masks, expected classification, saved settings and
sticky governance records. The wrapper mints a private one-use file token bound
to the destination env dir; the promote recipe consumes it before writing.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import stat
import sys
import tempfile
from pathlib import Path

ENV_VAR = "GENIERAILS_PROMOTE_TO_TOKEN"


def mint(env_dir: Path) -> Path:
    fd, name = tempfile.mkstemp(prefix=".genierails-promote-to-")
    with os.fdopen(fd, "w") as handle:
        json.dump({"env_dir": str(env_dir.resolve()), "issuer": os.getpid()}, handle)
        handle.write("\n")
    return Path(name)


def _parent_pid(pid: int) -> int | None:
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], text=True,
                             capture_output=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    try:
        return int(out)
    except ValueError:
        return None


def _in_ancestry(issuer: int) -> bool:
    pid = os.getpid()
    for _ in range(64):
        if pid == issuer:
            return True
        parent = _parent_pid(pid)
        if parent is None or parent <= 0 or parent == pid:
            return False
        pid = parent
    return False


def consume(env_dir: Path, token: str | None) -> bool:
    if not token:
        return False
    path = Path(token)
    try:
        info = path.lstat()
    except OSError:
        return False
    valid = (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
             and stat.S_IMODE(info.st_mode) == 0o600)
    try:
        data = json.loads(path.read_text())
        valid = (valid and data.get("env_dir") == str(env_dir.resolve())
                 and isinstance(data.get("issuer"), int)
                 and _in_ancestry(data["issuer"]))
    except (OSError, json.JSONDecodeError):
        valid = False
    if valid:
        path.unlink()
    return valid


def run(env_dir: Path, command: list[str]) -> int:
    token = mint(env_dir)
    env = os.environ | {ENV_VAR: str(token)}
    try:
        return subprocess.run(command, env=env).returncode
    finally:
        token.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("mint", "consume"):
        child = sub.add_parser(name)
        child.add_argument("--env-dir", required=True, type=Path)
    runner = sub.add_parser("run")
    runner.add_argument("--env-dir", required=True, type=Path)
    runner.add_argument("make_command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command == "mint":
        print(mint(args.env_dir))
        return 0
    if args.command == "run":
        command = args.make_command[1:] if args.make_command[:1] == ["--"] else args.make_command
        if not command:
            print("promote_token.py run: missing command", file=sys.stderr)
            return 2
        return run(args.env_dir, command)
    return 0 if consume(args.env_dir, os.environ.get(ENV_VAR)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
