"""Stub GenieRails scripts below make, with a test-only python3 on PATH.

The Makefile's check, guard and gate helpers are ``override`` variables, so
a command-line or environment value can't replace them (no ``make
_DEPLOYER_CHECK=true``). Tests stub one level lower instead: ``install``
writes a ``python3`` (and ``python``) into a temp bin dir that runs a stub
for each named script and the real interpreter for everything else. Put the
dir first on PATH. A real checkout never has this file on PATH.

Keys are a script's file name (``coverage_gate.py``) or a file name and its
first argument (``staged_policy_apply.py confirm``, checked first). Values are
the command that runs instead, given the arguments after the key (after the
script, or after the script and its first argument), the way the Makefile
helper they replace passed them: ``true``, a recorder executable, or
``python_file(fake)``.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Mapping


def install(bin_dir: Path, stubs: Mapping[str, str]) -> Path:
    bin_dir = Path(bin_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    real = shlex.quote(sys.executable)
    with_arg = {k: v for k, v in stubs.items() if " " in k}
    by_name = {k: v for k, v in stubs.items() if " " not in k}
    lines = [
        "#!/bin/sh",
        "# Test-only python3 (tests/script_stubs.py): stubs for the scripts below, the real interpreter otherwise.",
        'case "$1" in -*|"") exec ' + real + ' "$@" ;; esac',
        'name=${1##*/}',
    ]
    if with_arg:
        lines.append('case "$name ${2:-}" in')
        lines += [f"  {shlex.quote(key)}) shift 2; exec {command} \"$@\" ;;" for key, command in with_arg.items()]
        lines.append("esac")
    if by_name:
        lines.append('case "$name" in')
        lines += [f"  {shlex.quote(key)}) shift; exec {command} \"$@\" ;;" for key, command in by_name.items()]
        lines.append("esac")
    lines.append(f'exec {real} "$@"')
    for name in ("python3", "python"):
        path = bin_dir / name
        path.write_text("\n".join(lines) + "\n")
        path.chmod(0o755)
    return bin_dir


def python_file(path: Path) -> str:
    """A stub command that runs a fake Python script on the real interpreter."""
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(path))}"
