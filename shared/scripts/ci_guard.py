#!/usr/bin/env python3
"""CI detection for make's applying-target guards, in one place.

Makefile.shared's _guard-not-ci-apply and _guard-not-ci-rebind (and the
other recipes that ask "is this CI?") run this script; staged_policy_apply
and env_status (make next) import it, so what make refuses and what make next
suggests can't drift apart.

CI is ``CI`` set to true, 1 or yes (any case), or any of CI_MARKERS set to a
non-empty value. In CI the applying targets refuse, except:

- ``GENIERAILS_ALLOW_CI_APPLY=1``: an isolated integration-test workflow opts
  out (never for policy-rebind);
- ``make rehearse ENV=dev`` with configured, locked remote state
  (state_layout.ci_allowed).

Commands. Each answers with exactly one versioned line on stdout (ANSWERS),
and an exit code that must go with it. Callers accept only an exact (exit
code, line) pair from ANSWERS and refuse anything else: a missing, crashing
or unrelated script, one exiting with a lucky code, empty, extra or garbled
output (never a bare shell ``if`` on an exit code).
  apply --cloud-dir D --cloud-root R --env-dir E --env N   _guard-not-ci-apply: ALLOWED (0) or REFUSED (1)
  rebind                                                     _guard-not-ci-rebind: ALLOWED (0) or REFUSED (1)
  ci-status [--allow-opt-out]   STATUS_CI or STATUS_NOT_CI (opted out counts as not CI), both exit 0
An internal error prints no answer and exits 3.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable, Mapping

CI_MARKERS = ("TF_BUILD", "JENKINS_URL", "GITLAB_CI", "BUILDKITE", "CIRCLECI")
CI_VALUES = ("true", "1", "yes")
ERROR = 3
# The answers, as Makefile.shared's _CI_STATUS and _CI_GUARD_ANSWER match them (exit code:line).
STATUS_CI = "genierails-ci-status:v1:ci"
STATUS_NOT_CI = "genierails-ci-status:v1:not-ci"
ALLOWED = "genierails-ci-guard:v1:allowed"
REFUSED = "genierails-ci-guard:v1:refused"
ANSWERS = {"ci-status": {(0, STATUS_CI), (0, STATUS_NOT_CI)}, "apply": {(0, ALLOWED), (1, REFUSED)},
           "rebind": {(0, ALLOWED), (1, REFUSED)}}
ALLOW_CI_APPLY = "GENIERAILS_ALLOW_CI_APPLY"
APPLY_REFUSED = (
    "ERROR: applying targets cannot run in CI (except make rehearse ENV=dev with configured remote state and "
    "locking). Prod and account applies stay on the deployment machine.\n"
    f"Only isolated integration-test workflows may opt out with {ALLOW_CI_APPLY}=1.")
REBIND_REFUSED = (f"ERROR: make policy-rebind cannot run in CI, even with {ALLOW_CI_APPLY}=1. Run it on the "
                  "deployment machine.")


def ci_markers(environ: Mapping[str, str] | None = None) -> list[str]:
    """What makes this CI: ``CI=<value>`` and each marker set (empty when it isn't CI)."""
    environ = os.environ if environ is None else environ
    found = [f"CI={environ['CI']}"] if str(environ.get("CI") or "").lower() in CI_VALUES else []
    return found + [name for name in CI_MARKERS if environ.get(name)]


def in_ci(environ: Mapping[str, str] | None = None) -> bool:
    return bool(ci_markers(environ))


def status_line(environ: Mapping[str, str] | None = None, *, allow_opt_out: bool = False) -> str:
    """ci-status's answer: STATUS_CI, or STATUS_NOT_CI (also when ``allow_opt_out`` and opted out)."""
    return STATUS_CI if in_ci(environ) and not (allow_opt_out and opted_out(environ)) else STATUS_NOT_CI


def opted_out(environ: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if environ is None else environ
    return environ.get(ALLOW_CI_APPLY) == "1"


def apply_allowed(environ: Mapping[str, str] | None = None, *,
                  dev_rehearse: Callable[[], bool] = lambda: False) -> bool:
    """Whether _guard-not-ci-apply lets an applying target run (``dev_rehearse``: its ci_allowed exemption)."""
    return not in_ci(environ) or opted_out(environ) or dev_rehearse()


def dev_rehearse_allowed(env_dir: Path, env: str, cloud_dir: Path, cloud_root: Path | None) -> bool:
    """state_layout.ci_allowed, as the guard runs it; any error means not allowed."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from state_layout import ci_allowed

        return ci_allowed(env_dir, env, cloud_dir, cloud_root)
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    apply = sub.add_parser("apply")
    apply.add_argument("--cloud-dir", type=Path, required=True)
    apply.add_argument("--cloud-root", type=Path, required=True)
    apply.add_argument("--env-dir", type=Path, required=True)
    apply.add_argument("--env", required=True)
    sub.add_parser("rebind")
    check = sub.add_parser("ci-status")
    check.add_argument("--allow-opt-out", action="store_true")
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except Exception as exc:  # fail closed: an error is never "not CI"
        print(f"ERROR: scripts/ci_guard.py {args.command} failed ({type(exc).__name__}: {exc}); refusing.",
              file=sys.stderr)
        return ERROR


def _run(args) -> int:
    if args.command == "ci-status":
        print(status_line(allow_opt_out=args.allow_opt_out))
        return 0
    if args.command == "rebind":
        allowed, message = not in_ci(), REBIND_REFUSED
    else:
        allowed = apply_allowed(dev_rehearse=lambda: dev_rehearse_allowed(args.env_dir, args.env, args.cloud_dir,
                                                                           args.cloud_root))
        message = APPLY_REFUSED
    if not allowed:
        print(message, file=sys.stderr)
    print(ALLOWED if allowed else REFUSED)
    return 0 if allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
