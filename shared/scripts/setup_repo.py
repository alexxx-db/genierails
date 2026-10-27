#!/usr/bin/env python3
"""make setup's git remotes: REPO=<url> makes the checkout the customer's repo.

GenieRails manages git for the champion (owner decision "option A"): only the
git binary is needed, no GitHub account and no git knowledge. make setup runs
this twice: ``--check`` first, right after the scheduled-governance guard and
before anything is written (a refusal leaves the checkout and its remotes
exactly as they were), then again after the env files exist, to change the
remotes. It never fetches and never pushes.

With ``--repo <url>`` (make setup ENV=<env> REPO=<url>; only a command-line
REPO counts, so an exported REPO, such as bootstrap-pipelines' <owner>/<name>,
is ignored):
  - origin is GenieRails (github.com/databricks-solutions/genierails) and no
    ``genierails`` remote exists: rename origin to ``genierails`` (kept for
    upgrades), then add origin = <url>;
  - no origin (a local-only repo): add origin = <url>;
  - origin is already <url> (ignoring a trailing / or .git): nothing to do;
  - not a git checkout (a downloaded archive): git init the checkout root
    (the cloud folder's parent) on branch main, then add origin = <url>;
  - anything else (origin elsewhere, or GenieRails with a genierails remote
    already there) is refused: setup never repoints a remote.
Without it, a fresh clone (origin at GenieRails, no genierails remote) becomes
a local-only repo: origin is renamed to ``genierails`` and no origin is added,
so make setup writes the .gitignore block and envs/ is committed locally. That
rename is skipped with GITIGNORE=off (contributors working on GenieRails
itself) and in CI. Every other layout is left alone.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SHARED = Path(__file__).resolve().parents[1]
for _path in (SHARED, SHARED / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts.ci_guard import in_ci  # noqa: E402
from scripts.customer_gitignore import _is_upstream, _remotes  # noqa: E402

UPSTREAM_REMOTE = "genierails"
BRANCH = "main"
MANUAL = ("Prefer to manage remotes yourself? Leave REPO= off and use git remote "
          "(make setup only reads the origin remote).")


class Refused(Exception):
    pass


def _same(a: str, b: str) -> bool:
    def norm(url: str) -> str:
        return url.strip().rstrip("/").removesuffix(".git").rstrip("/")
    return norm(a) == norm(b)


def plan(remotes: dict[str, str] | None, repo: str, *, auto: bool = True) -> list[tuple[str, ...]]:
    """The git steps setup takes for ``remotes`` (None outside git); raises Refused, changing nothing."""
    if repo:
        if repo != repo.strip() or any(c.isspace() for c in repo) or repo.startswith("-"):
            raise Refused(f"REPO={repo!r} is not a git URL (no spaces, no leading -).")
        if _is_upstream(repo):
            raise Refused("REPO= is GenieRails itself; pass the URL of your own (empty) repo. "
                          "GenieRails stays reachable as the genierails remote.")
        if remotes is None:
            return [("init",), ("add", "origin", repo)]
        origin = remotes.get("origin")
        if origin is None:
            return [("add", "origin", repo)]
        if _same(origin, repo):
            return []
        if _is_upstream(origin) and UPSTREAM_REMOTE not in remotes:
            return [("rename", "origin", UPSTREAM_REMOTE), ("add", "origin", repo)]
        if _is_upstream(origin):
            raise Refused(f"origin is GenieRails and a {UPSTREAM_REMOTE} remote already exists "
                          f"({remotes[UPSTREAM_REMOTE]}); setup won't guess which to keep. Either:\n"
                          f"         git remote remove origin   (keeps {UPSTREAM_REMOTE}), then re-run with REPO=, or\n"
                          f"         git remote set-url origin {repo}")
        raise Refused(f"origin already points at {origin}, not {repo}; make setup never repoints a remote. "
                      f"Re-run with that REPO (or none), or, to move this checkout to {repo} on purpose:\n"
                      f"         git remote set-url origin {repo}")
    if (auto and remotes is not None and _is_upstream(remotes.get("origin"))
            and UPSTREAM_REMOTE not in remotes):
        return [("rename", "origin", UPSTREAM_REMOTE)]
    return []


def _git(cwd: Path, *args: str) -> None:
    proc = subprocess.run(["git", "-C", str(cwd), *args], text=True, capture_output=True)
    if proc.returncode:
        raise Refused(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")


def apply(cloud_dir: Path, steps: list[tuple[str, ...]]) -> list[str]:
    done = []
    for step in steps:
        if step[0] == "init":
            root = cloud_dir.resolve().parent
            _git(root, "init", "-q")
            _git(root, "symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
            done.append(f"initialised a git repo in {root} (branch {BRANCH})")
        elif step[0] == "rename":
            _git(cloud_dir, "remote", "rename", step[1], step[2])
            done.append(f"renamed remote {step[1]} -> {step[2]} (GenieRails, kept for upgrades)")
        else:
            _git(cloud_dir, "remote", "add", step[1], step[2])
            done.append(f"added remote {step[1]} = {step[2]}")
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cloud-dir", required=True, type=Path)
    parser.add_argument("--repo", default="")
    parser.add_argument("--gitignore", default="", help="make setup's GITIGNORE; off skips the no-REPO rename")
    parser.add_argument("--env", default="dev")
    parser.add_argument("--check", action="store_true", help="refuse or pass; change nothing")
    args = parser.parse_args(argv)
    remotes = _remotes(args.cloud_dir)
    auto = args.gitignore != "off" and not in_ci()
    try:
        steps = plan(remotes, args.repo, auto=auto)
        if args.check:
            return 0
        done = apply(args.cloud_dir, steps)
    except Refused as exc:
        print(f"ERROR: make setup REPO=: {exc}" if args.repo else f"ERROR: make setup: {exc}", file=sys.stderr)
        if args.repo:
            print(f"       {MANUAL}", file=sys.stderr)
        return 1
    for line in done:
        print(f"Git: {line}")
    if args.repo and not steps:
        print(f"Git: origin is already {remotes.get('origin')}; nothing to change.")
    elif done and not args.repo:
        print(f"Git: this is now a local-only repo (no origin): commit envs/ here; nothing is pushed. "
              f"Add your own repo later with make setup ENV={args.env} REPO=<url>. "
              f"Undo: git remote rename {UPSTREAM_REMOTE} origin")
    elif done:
        print(f"Git: nothing was pushed; the first push is git push -u origin {BRANCH}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
