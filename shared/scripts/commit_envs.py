#!/usr/bin/env python3
"""make generate and make promote-to offer to commit what they wrote (option A).

After a successful run, ``offer`` shows ``git diff --stat`` for exactly
``.gitignore`` and ``envs/<env>/`` (the cloud folder's) and asks y/N. On y it
stages only those paths (never ``git add -A``), commits them with a fixed
message (the repo's commit hooks run) and, when the ``origin`` remote
(RELEASE_REMOTE) exists, pushes HEAD to its main (RELEASE_BRANCH): never
forced, so a non-fast-forward push is refused and the commit stays local. With
no remote (a local-only repo) it commits locally only and says so.

When it asks (``will_offer``):
  - GENIERAILS_COMMIT is ``ask`` or ``1``: the top-level make sets it for make
    generate and make promote-to only, from a command-line COMMIT (unset: ask,
    COMMIT=1: 1, COMMIT=0: 0), and to 0 for every other goal, CHECK=1 and an
    inherited 0; nested makes inherit it. Outside make it is unset: never;
  - not CI (scripts/ci_guard.py): CI never commits, COMMIT=1 included;
  - stdin and stdout are a terminal, or COMMIT=1 (commit without asking);
  - the cloud folder is the top of its own git checkout (setup_repo.checkout),
    on RELEASE_BRANCH, and its .gitignore has the GenieRails customer block,
    which keeps credentials and runtime files out of envs/ (GenieRails' own repo
    ignores envs/ and is never offered);
  - make promote-to: the destination is deterministic.
Otherwise it writes nothing to git and prints nothing (non-TTY, CI and COMMIT=0
behave exactly as before), except a one-line reason on a terminal.

make can't tell a COMMIT typed on the command line from one inherited through
an exported MAKEFLAGS, so both count; a COMMIT in the shell environment never
does. Like setup's REPO=, this is an accident guard, not a security boundary.

Refused, with nothing committed (exit 1):
  - something outside .gitignore and envs/<env>/ is already staged, or a merge,
    rebase, cherry-pick or revert is in progress;
  - a path it would commit is credentials, Terraform state or plans, a runtime
    or evidence file (DENIED, or git-ignored by the .gitignore rules even though
    tracked), or holds a client secret or token value (SECRET_RE).
The preview stages into a temporary copy of the index, so answering n (or a
refusal) leaves the index, HEAD and the work tree exactly as they were; a
failed commit restores the index.

Exit codes: 0 committed, declined, nothing to commit or not offered; 1 refused
or the push failed (the commit then stays local); 2 usage error.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Mapping

SHARED = Path(__file__).resolve().parents[1]
for _path in (SHARED, SHARED / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import ci_guard  # noqa: E402
import customer_gitignore  # noqa: E402
import setup_repo  # noqa: E402

STEPS = {
    "generate": "GenieRails: draft the {env} rules (make generate ENV={env})",
    "promote-to": "GenieRails: promote the rules to {env} (make promote-to ENV={env})",
}
# What the customer .gitignore block keeps out (customer_gitignore.COMMON), checked
# again on every path about to be committed, whatever .gitignore says now.
DENIED_NAMES = {"auth.auto.tfvars": "credentials", "classification.auto.tfvars": "a runtime file",
                "discovered_uc_tables.auto.tfvars": "a runtime file"}
DENIED_DIRS = {".terraform": "Terraform's working directory", ".genierails-state": "the receipt store",
               "__pycache__": "Python bytecode", "evidence": "an evidence record"}
SECRET_RE = re.compile(
    r"""(?mi)^[ \t]*"?(?:databricks_client_secret|azure_client_secret|client_secret|databricks_token)"?"""
    r"""[ \t]*[=:][ \t]*"[^"\s]+"|\bdapi[0-9a-f]{32}\b""")


def _git_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in setup_repo._GIT_LOCATION_VARS}
    env.update(extra)
    return env


def _git(cwd: Path, *args: str, index: str = "", stdin: str | None = None) -> subprocess.CompletedProcess:
    env = _git_env(GIT_INDEX_FILE=index) if index else _git_env()
    return subprocess.run(["git", "-C", str(cwd), *args], text=True, capture_output=True, env=env, input=stdin)


def mode(environ: Mapping[str, str], interactive: bool) -> str:
    """"ask", "auto" (COMMIT=1) or "" (no git writes, no output)."""
    commit = environ.get("GENIERAILS_COMMIT", "0")
    if commit not in ("ask", "1") or ci_guard.in_ci(environ):
        return ""
    if commit == "1":
        return "auto"
    return "ask" if interactive else ""


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _deterministic(env_dir: Path) -> bool:
    try:
        import hcl2
        return hcl2.loads((env_dir / "env.auto.tfvars").read_text()).get("governance_mode") == "deterministic"
    except Exception:
        return False


def unusable(cloud_dir: Path, env: str, env_dir: Path, step: str, branch: str) -> str:
    """Why this checkout can't take the commit ("" when it can)."""
    if Path(env_dir).resolve() != (cloud_dir / "envs" / env).resolve():
        return f"ENV_DIR is not {cloud_dir.name}/envs/{env}"
    root, remotes, why = setup_repo.checkout(cloud_dir)
    if why:
        return why
    if remotes is None:
        return f"{root} is not a git checkout"
    try:
        has_block = customer_gitignore._split((cloud_dir / ".gitignore").read_text())[1] is not None
    except (OSError, ValueError):
        has_block = False
    if not has_block:
        return (f"{cloud_dir.name}/.gitignore has no GenieRails customer block, so envs/ isn't committed here "
                f"(make setup ENV={env} writes it in your own repo)")
    if step == "promote-to" and not _deterministic(env_dir):
        return f"{env} is not deterministic"
    head = _git(root, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    if head != branch:
        return f"HEAD is {'detached' if not head else 'on ' + head}, not {branch}"
    return ""


def will_offer(cloud_dir: Path, env: str, env_dir: Path, step: str, *, environ: Mapping[str, str] | None = None,
               tty: bool | None = None, branch: str = "main") -> bool:
    """Whether offer would ask (or, with COMMIT=1, commit): what the banners print."""
    environ = os.environ if environ is None else environ
    return bool(mode(environ, interactive() if tty is None else tty)) and not unusable(
        Path(cloud_dir), env, Path(env_dir), step, branch)


def denied(rel: str) -> str:
    """Why a cloud-relative path must never be committed ("" when it may)."""
    parts = PurePosixPath(rel).parts
    name = parts[-1]
    if name in DENIED_NAMES:
        return DENIED_NAMES[name]
    if name.endswith((".tfstate", ".tfstate.backup", ".tfplan")) or ".tfstate." in name:
        return "Terraform state or a plan"
    for part in parts[:-1]:
        if part in DENIED_DIRS:
            return DENIED_DIRS[part]
    if parts[0] == "envs" and any(part.startswith(".") for part in parts[1:]):
        return "a runtime file (envs/**/.*)"
    if rel.endswith("/ddl/_fetched.sql") or (len(parts) == 4 and parts[2] == "data_access"
                                              and name == "env.auto.tfvars"):
        return "a runtime file"
    return ""


def _staged(root: Path, index: str = "") -> list[tuple[str, str]]:
    """(status letter, repo-relative path) of every staged change."""
    out = _git(root, "diff", "--cached", "--name-status", "--no-renames", "-z", index=index)
    if out.returncode:
        raise RuntimeError((out.stderr or out.stdout).strip())
    fields = out.stdout.split("\0")
    return [(fields[i], fields[i + 1]) for i in range(0, len(fields) - 1, 2)]


def _problems(root: Path, cloud_rel: str, staged: list[tuple[str, str]], index: str) -> list[str]:
    problems = []
    for status, path in staged:
        rel = path[len(cloud_rel) + 1:] if cloud_rel else path
        why = denied(rel)
        if not why and not _git(root, "check-ignore", "-q", "--no-index", "--", path).returncode:
            why = "git-ignored by .gitignore (a runtime file), though tracked"
        if not why and status != "D":
            blob = _git(root, "show", f":{path}", index=index)
            if blob.returncode == 0 and SECRET_RE.search(blob.stdout):
                why = "it holds a client secret or token value"
        if why:
            problems.append(f"{path}: {why}")
    return problems


def _in_progress(root: Path) -> str:
    for name, what in (("MERGE_HEAD", "a merge"), ("rebase-merge", "a rebase"), ("rebase-apply", "a rebase"),
                       ("CHERRY_PICK_HEAD", "a cherry-pick"), ("REVERT_HEAD", "a revert")):
        path = _git(root, "rev-parse", "--git-path", name).stdout.strip()
        if path and (root / path).exists():
            return what
    return ""


def offer(cloud_dir: Path, env: str, env_dir: Path, step: str, *, remote: str = "origin",
          branch: str = "main", environ: Mapping[str, str] | None = None, tty: bool | None = None) -> int:
    environ = os.environ if environ is None else environ
    how = mode(environ, interactive() if tty is None else tty)
    if not how:
        return 0
    cloud_dir = Path(cloud_dir).resolve()
    why = unusable(cloud_dir, env, Path(env_dir), step, branch).rstrip(".")
    if why:
        if how == "auto":
            print(f"ERROR: COMMIT=1: not committing: {why}.", file=sys.stderr)
            return 1
        print(f"  (Not offering to commit: {why}.)")
        return 0
    root = cloud_dir.parent
    cloud_rel = cloud_dir.name
    paths = [f"{cloud_rel}/.gitignore", f"{cloud_rel}/envs/{env}"]
    manual = f"git add .gitignore envs/{env}/ && git commit"
    has_remote = _git(root, "remote", "get-url", remote).returncode == 0
    if has_remote:
        manual += f" && git push {remote} HEAD:{branch}"

    def refuse(message: str) -> int:
        print(f"ERROR: not committing: {message}\n  Nothing was staged or committed. Fix it, then: {manual}",
              file=sys.stderr)
        return 1

    busy = _in_progress(root)
    if busy:
        return refuse(f"{busy} is in progress.")
    allowed = (f"{cloud_rel}/.gitignore", f"{cloud_rel}/envs/{env}/")
    other = sorted(p for _s, p in _staged(root) if p != allowed[0] and not p.startswith(allowed[1]))
    if other:
        return refuse("other changes are already staged (" + ", ".join(other[:5])
                      + (", ..." if len(other) > 5 else "") + "); commit or unstage them first (git restore --staged).")
    real_index = root / _git(root, "rev-parse", "--git-path", "index").stdout.strip()
    with tempfile.TemporaryDirectory(prefix="genierails-commit-") as tmp:
        preview = str(Path(tmp) / "index")
        if real_index.exists():
            shutil.copy2(real_index, preview)
        added = _git(root, "add", "--", *paths, index=preview)
        if added.returncode:
            return refuse(f"git add failed: {(added.stderr or added.stdout).strip()}")
        staged = _staged(root, preview)
        if not staged:
            if how == "ask":
                print(f"  Nothing to commit: .gitignore and envs/{env}/ match HEAD.")
            return 0
        problems = _problems(root, cloud_rel, staged, preview)
        if problems:
            return refuse("these would be committed:\n    " + "\n    ".join(problems)
                          + "\n  The GenieRails .gitignore block keeps them out; a tracked one needs "
                          "git rm --cached <path>.")
        stat = _git(root, "diff", "--cached", "--stat", "--no-renames", "--", *paths, index=preview).stdout
    print(f"\n=== Commit {env}'s changes (.gitignore and envs/{env}/) ===")
    print(stat.rstrip("\n"))
    rest = sorted({line[3:] for line in _git(root, "status", "--porcelain").stdout.splitlines()
                   if not line[3:].strip('"').startswith(allowed) and line[3:].strip('"') != allowed[0]})
    if rest:
        print("  Not included (left as they are): " + ", ".join(rest[:5]) + (", ..." if len(rest) > 5 else ""))
    target = f"push them to {remote}/{branch}" if has_remote else f"keep them local (no {remote} remote)"
    if how == "ask":
        try:
            answer = input(f"Commit these changes and {target}? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print(f"  Not committed; git is unchanged. To commit yourself: {manual}")
            return 0
    saved = Path(tempfile.mkdtemp(prefix="genierails-index-"))
    try:
        if real_index.exists():
            shutil.copy2(real_index, saved / "index")

        def restore(message: str) -> int:
            if (saved / "index").exists():
                shutil.copy2(saved / "index", real_index)
            else:
                real_index.unlink(missing_ok=True)
            return refuse(message)

        added = _git(root, "add", "--", *paths)
        if added.returncode:
            return restore(f"git add failed: {(added.stderr or added.stdout).strip()}")
        staged_now = _staged(root)
        if staged_now != staged:
            return restore("the files changed while you answered; re-run the make command.")
        problems = _problems(root, cloud_rel, staged_now, "")
        if problems:
            return restore("these would be committed:\n    " + "\n    ".join(problems))
        commit = subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", STEPS[step].format(env=env)],
                                env=_git_env())
        if commit.returncode:
            return restore("git commit failed (see above).")
    finally:
        shutil.rmtree(saved, ignore_errors=True)
    sha = _git(root, "rev-parse", "--short", "HEAD").stdout.strip()
    print(f"  Committed {sha} on {branch}: .gitignore and envs/{env}/ ({len(staged)} file(s)).")
    if not has_remote:
        print(f"  No {remote} remote (a local-only repo): committed locally only; nothing was pushed.")
        return 0
    push = _git(root, "push", remote, f"HEAD:{branch}")
    if push.returncode == 0:
        print(f"  Pushed to {remote}/{branch}.")
        return 0
    detail = (push.stderr or push.stdout).strip()
    if "non-fast-forward" in detail or "fetch first" in detail or "[rejected]" in detail:
        reason = (f"{remote}/{branch} has commits this checkout doesn't (not a fast-forward), and GenieRails "
                  "never force-pushes. Bring them in with git pull --rebase " f"{remote} {branch}, then git push "
                  f"{remote} HEAD:{branch}; if {branch} is protected, push a branch and merge its PR instead")
    else:
        reason = f"git push failed: {detail}"
    print(f"ERROR: committed {sha} locally, but did not push: {reason}.", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("offer", "will-offer"))
    parser.add_argument("--cloud-dir", required=True, type=Path)
    parser.add_argument("--env", required=True)
    parser.add_argument("--env-dir", required=True, type=Path)
    parser.add_argument("--step", required=True, choices=sorted(STEPS))
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="main")
    args = parser.parse_args(argv)
    if args.command == "will-offer":
        return 0 if will_offer(args.cloud_dir, args.env, args.env_dir, args.step, branch=args.branch) else 1
    return offer(args.cloud_dir, args.env, args.env_dir, args.step, remote=args.remote, branch=args.branch)


if __name__ == "__main__":
    raise SystemExit(main())
