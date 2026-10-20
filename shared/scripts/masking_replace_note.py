#!/usr/bin/env python3
"""Explain a plan whose only destroys replace terraform_data.masking_functions.

Every masking SQL change replaces terraform_data.masking_functions, so plan
and apply report "must be replaced" and "1 to destroy". That replacement only
re-runs CREATE OR REPLACE FUNCTION; it never drops live mask functions. Reads
a copy of Terraform's plan or apply output (terraform_layer.sh tees it; the
output itself is never filtered) and prints one note when every resource the
plan destroys is such a replacement (or the policy-enforcement wait it
restarts). When the replacement's only changed triggers are the checkout
paths older states recorded (sql_file, script, auth_file), it says so: that
one-time re-key redeploys the same functions. Never fails.

Usage: python3 masking_replace_note.py <terraform-output-file>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

NOTE = ("Note: replacing terraform_data.masking_functions only re-runs CREATE OR REPLACE; "
        "it never drops live mask functions.")
REKEY_NOTE = ("Note: this replacement only stops recording checkout paths (sql_file, script, auth_file) "
              "in its triggers; the SQL, warehouse, host and client are unchanged, so it redeploys the same "
              "functions. It happens once, on the first plan after the upgrade.")

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_ACTION = re.compile(
    r"^\s*# (\S+)(?: \(deposed object \S+\))? "
    r"(will be destroyed|must be replaced|is tainted, so must be replaced)\s*$"
)
_PLAN = re.compile(r"^Plan: \d+ to add, \d+ to change, (\d+) to destroy\.")
_MASKING = re.compile(r"(?:^|\.)terraform_data\.masking_functions(?:\[[^\]]*\])?$")
# Replaced whenever masking_functions is (it is keyed on its id); it changes no access.
_WAIT = re.compile(r"(?:^|\.)time_sleep\.wait_for_policy_enforcement(?:\[[^\]]*\])?$")
# Any other resource's header ("# (4 unchanged attributes hidden)" is not one).
_HEADER = re.compile(r"^\s*# \S+\.\S+ (?:will|must|is|has) ")
_CHANGED = re.compile(r'^\s*[~+-]\s+"?([A-Za-z_]\w*)"?\s+=')
PATH_KEYS = {"sql_file", "script", "auth_file"}


def _actions(lines: list[str]) -> tuple[list[tuple[str, str]], int | None]:
    actions: list[tuple[str, str]] = []
    destroy_count = None
    for raw in lines:
        line = _ANSI.sub("", raw).rstrip("\n")
        if match := _ACTION.match(line):
            actions.append((match.group(1), match.group(2)))
        elif match := _PLAN.match(line.strip()):
            destroy_count = int(match.group(1))
    return actions, destroy_count


def needs_note(lines: list[str]) -> bool:
    """True when the plan destroys something and every destroy is a
    masking_functions replacement or the wait it restarts (and the plan
    summary agrees)."""
    actions, destroy_count = _actions(lines)
    if not actions or destroy_count != len(actions):
        return False
    return (any(_MASKING.search(address) for address, _ in actions)
            and all(action.endswith("must be replaced") and (_MASKING.search(address) or _WAIT.search(address))
                    for address, action in actions))


def is_rekey(lines: list[str]) -> bool:
    """True when every masking_functions change shown is a checkout-path trigger."""
    changed: set[str] = set()
    inside = False
    for raw in lines:
        line = _ANSI.sub("", raw).rstrip("\n")
        if match := _ACTION.match(line):
            inside = bool(_MASKING.search(match.group(1)))
        elif _HEADER.match(line) or _PLAN.match(line.strip()):
            inside = False
        elif inside and (match := _CHANGED.match(line)):
            changed.add(match.group(1))
    changed -= {"id", "triggers_replace"}
    return bool(changed) and changed <= PATH_KEYS


def main(argv: list[str]) -> int:
    try:
        lines = Path(argv[1]).read_text(errors="replace").splitlines()
    except (IndexError, OSError):
        return 0
    if needs_note(lines):
        print(NOTE)
        if is_rekey(lines):
            print(REKEY_NOTE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
