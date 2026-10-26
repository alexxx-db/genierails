#!/usr/bin/env python3
"""Refuse state-changing make targets on Databricks compute.

A notebook, Job, serverless run or App keeps no local disk past the run, but
the receipts, manifests, the staged policy journal, env locks and Genie ID
records a state-changing target writes live on local disk. Running one there
would change live resources and then lose the records of it, so make's
_guard-not-ci-apply and _guard-not-databricks-compute run this first, before
any lock, write or live call. It imports nothing but the standard library and
never calls Databricks.

Markers (any one means Databricks compute):

  DATABRICKS_RUNTIME_VERSION  Databricks Runtime notebooks and jobs; the
                              Databricks SDK gates its notebook-native auth
                              on it (databricks/sdk/credentials_provider.py)
  DB_CLUSTER_ID               classic all-purpose and job clusters (the
  DB_IS_JOB_CLUSTER           documented default cluster environment variables)
  DATABRICKS_APP_NAME         Databricks Apps (a documented default variable)
  /Workspace mount            the workspace files FUSE mount on Databricks
                              Runtime and serverless compute: an exact-case
                              /Workspace entry under / that is a mount point,
                              so serverless is caught even without a variable

An environment marker counts only when set and non-empty. Credentials-only
variables (DATABRICKS_HOST, DATABRICKS_CLIENT_ID, ...) and Databricks Connect's
client settings (DATABRICKS_CLUSTER_ID, DATABRICKS_SERVERLESS_COMPUTE_ID) are not
markers: deployment machines and laptops set them too. A laptop has no
/Workspace mount (a devcontainer's is /workspaces or /workspace, not this name).

Opt-in for a future Job runner, NOT supported end to end yet: make passes
--opt-in 1 only when GENIERAILS_ALLOW_DATABRICKS_COMPUTE=1 has origin
"command line" (a shell variable or make -e never counts), and
--opt-in-from-makeflags 1 when, on Linux, the top-level make inherited that
setting through an exported MAKEFLAGS (refused too). Both must be paired with
GENIERAILS_DURABLE_STATE_DIR: an absolute path to an existing directory that
holds GENIERAILS_STATE_DIR, so state and records land on the durable volume.

  databricks_compute.py guard [--goals "<make goals>" | --what "<command>"] [--opt-in 1]
                              [--opt-in-from-makeflags 1]

Detection stays self-contained here (one MARKERS tuple, one marker()) so it can
fold into a single CI/compute detector later.

Exit 0: not Databricks compute, or a complete opt-in. Exit 1: refused.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

MARKERS = ("DATABRICKS_RUNTIME_VERSION", "DB_CLUSTER_ID", "DB_IS_JOB_CLUSTER", "DATABRICKS_APP_NAME")
OPT_IN = "GENIERAILS_ALLOW_DATABRICKS_COMPUTE"
DURABLE_DIR = "GENIERAILS_DURABLE_STATE_DIR"
STATE_DIR = "GENIERAILS_STATE_DIR"
READ_ONLY = "make check, plan, validate, evidence and audit-schema"


WORKSPACE_MOUNT = "Workspace"


def marker(environ, root: Path = Path("/")) -> str | None:
    """The first Databricks compute marker: a variable set in ``environ``, or the /Workspace mount."""
    found = next((name for name in MARKERS if environ.get(name)), None)
    if found:
        return found
    try:
        # Exact case, so a case-insensitive disk's /workspace never counts.
        if WORKSPACE_MOUNT in os.listdir(root) and os.path.ismount(root / WORKSPACE_MOUNT):
            return f"the {root / WORKSPACE_MOUNT} mount"
    except OSError:
        pass
    return None


def _opt_in_gap(environ, opt_in: str, from_makeflags: str) -> str | None:
    """Why the opt-in is incomplete, or None when both parts are in place."""
    if from_makeflags == "1":
        return (f"{OPT_IN}=1 came from an exported MAKEFLAGS; pass it on the make command line itself "
                "(unset MAKEFLAGS)")
    if opt_in != "1":
        return (f"the opt-in needs {OPT_IN}=1 on the make command line (a shell variable, an exported "
                "variable or make -e doesn't count)")
    durable = environ.get(DURABLE_DIR, "").strip()
    if not durable:
        return f"{OPT_IN}=1 also needs {DURABLE_DIR} naming an existing directory on durable storage"
    path = Path(durable)
    if not path.is_absolute() or not path.is_dir():
        return f"{DURABLE_DIR}={durable} must be an absolute path to an existing directory"
    state = environ.get(STATE_DIR, "").strip()
    durable_real = path.resolve()
    state_real = Path(state).expanduser().resolve() if state else None
    if state_real is None or not state_real.is_dir() or not (
            state_real == durable_real or durable_real in state_real.parents):
        return (f"{STATE_DIR} must be an existing directory inside {DURABLE_DIR} ({durable}), so state, "
                "receipts, manifests, locks and Genie ID records are written there")
    return None


def guard(environ, goals: str = "", opt_in: str = "", from_makeflags: str = "", what: str = "",
          root: Path = Path("/")) -> str | None:
    """The refusal message, or None when the target may run."""
    found = marker(environ, root)
    if found is None:
        return None
    attempted = opt_in or from_makeflags or environ.get(DURABLE_DIR, "").strip()
    gap = _opt_in_gap(environ, opt_in, from_makeflags)
    if gap is None:
        return None
    what = what.strip() or (f"make {goals.strip()}" if goals.strip() else "this state-changing target")
    seen = found if found.startswith("the ") else f"{found} is set"
    message = (f"ERROR: {what} refused: this is Databricks compute ({seen}). Receipts, manifests, the "
               "staged policy journal, env locks and Genie ID records live on local disk, which this compute "
               "doesn't keep after the run, so a live change made here would lose its records. Nothing was "
               "changed. Run it from a long-lived deployment machine (shared/docs/deployment-machine.md); "
               f"{READ_ONLY} still run here.")
    if attempted:
        message += (f"\nThe Databricks compute opt-in is incomplete: {gap}. It is not supported end to end "
                    "yet (shared/docs/pipelines.md#databricks-compute).")
    return message


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("guard")
    check.add_argument("--goals", default="")
    check.add_argument("--opt-in", default="")
    check.add_argument("--opt-in-from-makeflags", default="")
    check.add_argument("--what", default="")
    args = parser.parse_args(argv)
    message = guard(os.environ, args.goals, args.opt_in, args.opt_in_from_makeflags, args.what)
    if message:
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
