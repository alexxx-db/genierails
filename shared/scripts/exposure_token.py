#!/usr/bin/env python3
"""One-time proof that this run already derived assignments for an env.

rehearse, release and maintain derive (live refresh, external mask
inventory) and then run a nested apply. That nested apply skips its own
derive only when it is handed a token the parent minted after its derive
passed: a private regular file, owned by this user, naming the same env dir.
The only other issuer is make check's structural plan in its scratch copy,
which deliberately doesn't re-read live tags (a plan applies nothing).
The apply consumes (deletes) it, so it works once. No Make or environment
variable alone skips the derive; without a valid token it always runs.

  exposure_token.py mint --env-dir <env>      prints the token's path
  exposure_token.py consume --env-dir <env>   reads GENIERAILS_EXPOSURE_TOKEN;
                                              exit 0 = derived, skip; 1 = derive
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
import tempfile
from pathlib import Path

ENV_VAR = "GENIERAILS_EXPOSURE_TOKEN"


def mint(env_dir: Path) -> Path:
    fd, name = tempfile.mkstemp(prefix=".genierails-derived-")  # 0600, unpredictable name
    with os.fdopen(fd, "w") as handle:
        handle.write(str(env_dir.resolve()) + "\n")
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
        valid = valid and path.read_text() == str(env_dir.resolve()) + "\n"
    except OSError:
        valid = False
    if valid:
        path.unlink()
    return valid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("mint", "consume"))
    parser.add_argument("--env-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "mint":
        print(mint(args.env_dir))
        return 0
    return 0 if consume(args.env_dir, os.environ.get(ENV_VAR)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
