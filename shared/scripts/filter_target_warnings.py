#!/usr/bin/env python3
"""Drop Terraform's known-benign ``-target`` warnings from piped output.

``make enable-classification`` applies only the classification resource with
``-target``, so Terraform always prints "Resource targeting is in effect" and
"Applied changes may be incomplete". Both are expected there and alarm
first-time users. A diagnostic box is dropped only when its COMPLETE content
is one of those two warnings (compared with colour codes, box glyphs and line
wrapping removed). Any other box, including one with an error interleaved
into it from merged stderr, is emitted verbatim, as is every line outside a
box and an unterminated box at EOF. The known missing-usage-policy error also
gets a cloud-specific remediation hint after its unmodified diagnostic.

Usage: terraform ... 2>&1 | python3 -u filter_target_warnings.py --cloud aws
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Iterable, Iterator

# Terraform 1.x text of the two warnings (shared/tests/fixtures holds a capture).
BENIGN_TARGET_WARNINGS = (
    "Warning: Resource targeting is in effect "
    "You are creating a plan with the -target option, which means that the "
    "result of this plan may not represent all of the changes requested by the "
    "current configuration. "
    "The -target option is not for routine use, and is provided only for "
    "exceptional situations such as recovering from errors or mistakes, or when "
    "Terraform specifically suggests to use it as part of an error message.",
    "Warning: Applied changes may be incomplete "
    "The plan was created with the -target option in effect, so some changes "
    "requested in the configuration may have been ignored and the output values "
    "may not be fully updated. Run the following command to verify that no other "
    "changes are pending: terraform plan "
    "Note that the -target option is not suitable for routine use, and is "
    "provided only for exceptional situations such as recovering from errors or "
    "mistakes, or when Terraform specifically suggests to use it as part of an "
    "error message.",
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_OPEN = "╷"
_CLOSE = "╵"
_BAR = "│"
_USAGE_POLICY_ERROR = "Usage policy ID must not be empty"
_USAGE_POLICY_DOCS = {
    "aws": "https://docs.databricks.com/aws/en/admin/usage/budget-policies",
    "azure": "https://learn.microsoft.com/en-us/azure/databricks/admin/usage/budget-policies",
}


def _plain(line: str) -> str:
    return _ANSI.sub("", line).strip()


def _box_text(inner: list[str]) -> str | None:
    """Return normalized box text, or None if an inner line is not a box line."""
    words: list[str] = []
    for line in inner:
        plain = _plain(line)
        if not plain.startswith(_BAR):
            return None  # e.g. an error line interleaved from stderr
        words.extend(plain[len(_BAR):].split())
    return " ".join(words)


def _is_benign(inner: list[str]) -> bool:
    """True only if the complete box spells a known target warning."""
    return _box_text(inner) in BENIGN_TARGET_WARNINGS


def _is_usage_policy_error(inner: list[str]) -> bool:
    """True for a Terraform Error box containing the missing-policy signature."""
    text = _box_text(inner)
    return text is not None and text.startswith("Error:") and _USAGE_POLICY_ERROR in text


def filter_lines(
    lines: Iterable[str], hide_outputs: bool = False, cloud: str = "aws"
) -> Iterator[str]:
    """Yield lines minus benign target warnings and, optionally, final outputs."""
    block: list[str] | None = None
    in_outputs = False
    missing_usage_policy = False
    for line in lines:
        if hide_outputs and _plain(line) == "Outputs:":
            in_outputs = True
            continue
        if in_outputs:
            continue
        plain = _plain(line)
        if block is None:
            if plain == _OPEN:
                block = [line]
            else:
                yield line
            continue
        block.append(line)
        if plain == _CLOSE:
            inner = block[1:-1]
            if not _is_benign(inner):
                yield from block
            if _is_usage_policy_error(inner):
                missing_usage_policy = True
            block = None
    if block:  # unterminated box at EOF: never swallow it
        yield from block
    if missing_usage_policy:
        yield (
            "\nEnable classification in the UI, or create and attach a serverless "
            f"usage policy: {_USAGE_POLICY_DOCS[cloud]}\n"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cloud", choices=sorted(_USAGE_POLICY_DOCS), default="aws")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    for line in filter_lines(sys.stdin, hide_outputs=not args.verbose, cloud=args.cloud):
        sys.stdout.write(line)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
