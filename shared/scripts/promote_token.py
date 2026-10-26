#!/usr/bin/env python3
"""One-time accident guard for promote-to's nested cross-env promote.

The public make promote SOURCE_ENV=... DEST_ENV=... form is legacy-compatible,
but deterministic destinations must go through make promote-to so the wrapper
also promotes agents, custom masks, expected classification, saved settings and
sticky governance records. The wrapper mints a private one-use file token bound
to the destination env dir; the promote guard consumes it before writing.

This prevents an ambient Make/environment variable from accidentally skipping
the guard. Like exposure_token.py, it is not a security boundary against a
caller who can create owner-only files and deliberately reproduce the format.
"""

from __future__ import annotations

import argparse
import os
import signal
import stat
import sys
import tempfile
from pathlib import Path

ENV_VAR = "GENIERAILS_PROMOTE_TO_TOKEN"
TOKEN_PREFIX = "GENIERAILS_PROMOTE_TO_V1\n"


def mint(env_dir: Path) -> Path:
    fd, name = tempfile.mkstemp(prefix=".genierails-promote-to-")
    with os.fdopen(fd, "w") as handle:
        handle.write(TOKEN_PREFIX + str(env_dir.resolve()) + "\n")
    return Path(name)


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
        valid = valid and path.read_text() == TOKEN_PREFIX + str(env_dir.resolve()) + "\n"
    except OSError:
        valid = False
    if valid:
        path.unlink()
    return valid


def run(env_dir: Path, command: list[str]) -> int:
    token = mint(env_dir)
    env = os.environ | {ENV_VAR: str(token)}
    child = None

    def terminate(signum, _frame):
        if child is not None and child.poll() is None:
            child.terminate()
        raise SystemExit(128 + signum)

    previous = {sig: signal.signal(sig, terminate) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        import subprocess
        child = subprocess.Popen(command, env=env)
        return child.wait()
    finally:
        token.unlink(missing_ok=True)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


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
