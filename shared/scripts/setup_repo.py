#!/usr/bin/env python3
"""make setup's git remotes: REPO=<url> makes the checkout the customer's repo.

GenieRails manages git for the champion (owner decision "option A"): only the
git binary is needed, no GitHub account and no git knowledge. make setup runs
this twice: ``--check`` first, right after the scheduled-governance guard and
before anything is written (a refusal leaves the checkout and its remotes
exactly as they were), then again after the env files exist, to change the
remotes. It never fetches and never pushes.

The URL comes from the GENIERAILS_SETUP_REPO environment variable, which make
exports from a command-line REPO (``$(value REPO)``, so neither make nor the
shell ever parses it). make can't tell a REPO typed on the command line from
one inherited through MAKEFLAGS, so both count; a plain exported REPO (such
as bootstrap-pipelines' <owner>/<name>) doesn't. Like REUSE_SOURCE_AUTH, this
is an accident guard, not a security boundary.

Only https://, ssh://, file://, scp-like ``[user@]host:path`` and absolute
local paths are accepted: never a ``<transport>::`` helper (ext::, fd::, ...),
control characters, spaces or shell metacharacters.

git runs at the GenieRails checkout root, the cloud folder's parent, which must
hold shared/ and be the top of its own repo (GIT_DIR, GIT_WORK_TREE and the
like are scrubbed). A cloud folder inside some other repo (GenieRails vendored
into an outer project) is refused, never touching the outer repo's remotes.

With a REPO:
  - origin is GenieRails (github.com/databricks-solutions/genierails) and no
    ``genierails`` remote exists: rename origin to ``genierails`` (kept for
    upgrades), then add origin = <url>;
  - no origin (a local-only repo): add origin = <url>;
  - origin is already <url> (the same host and path: https and ssh forms,
    with or without .git or a trailing /): nothing to do;
  - not a git checkout (a downloaded archive): git init the checkout root on
    branch main, then add origin = <url>;
  - anything else (origin elsewhere, or GenieRails with a genierails remote
    already there) is refused: setup never repoints a remote.
Without it, a fresh clone (origin at GenieRails, no genierails remote) becomes
a local-only repo: origin is renamed to ``genierails`` and no origin is added,
so make setup writes the .gitignore block and envs/ is committed locally. That
rename is skipped with GITIGNORE=off (contributors working on GenieRails
itself), in CI, and inside another repo. Every other layout is left alone.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

SHARED = Path(__file__).resolve().parents[1]
for _path in (SHARED, SHARED / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts.ci_guard import in_ci  # noqa: E402
from scripts.customer_gitignore import _is_upstream  # noqa: E402

REPO_ENV = "GENIERAILS_SETUP_REPO"
UPSTREAM_REMOTE = "genierails"
BRANCH = "main"
MANUAL = ("Prefer to manage remotes yourself? Leave REPO= off and use git remote "
          "(without REPO, setup only renames an origin that still points at GenieRails).")
# Variables that would point git at another repo or work tree than the checkout root.
_GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                      "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES",
                      "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_PREFIX")
_SAFE_CHARS = re.compile(r"[A-Za-z0-9._~:/@%+=,\[\]-]+")
_SCP = re.compile(r"(?:(?P<user>[A-Za-z0-9._-]+)@)?(?P<host>[A-Za-z0-9.-]+):(?P<path>[^:].*)")


class Refused(Exception):
    pass


def validate(repo: str) -> None:
    """Refuse anything but https://, ssh://, file://, scp-like and absolute local paths."""
    if any(ord(c) < 32 or ord(c) == 127 for c in repo):
        raise Refused("REPO= contains a control character (a newline?); pass one plain git URL.")
    if not _SAFE_CHARS.fullmatch(repo):
        raise Refused(f"REPO={repo!r} has characters a git URL here never needs (spaces, quotes, $, `, ;, |, &, "
                      "...); pass one plain https://, ssh:// or git@host:owner/repo URL.")
    if re.match(r"[^/]*::", repo):
        raise Refused(f"REPO={repo!r} names a git remote helper (<transport>::...); "
                      "only https://, ssh://, file://, git@host:path and absolute paths are accepted.")
    if "://" in repo:
        scheme = repo.split("://", 1)[0].lower()
        if scheme not in ("https", "ssh", "file"):
            raise Refused(f"REPO={repo!r}: {scheme}:// isn't accepted; use https://, ssh:// or file://.")
        return
    if repo.startswith("/") or _SCP.fullmatch(repo):
        return
    raise Refused(f"REPO={repo!r} is not a git URL: use https://host/owner/repo, ssh://..., "
                  "git@host:owner/repo or an absolute path.")


def _key(url: str) -> tuple[str, str]:
    """(host, path) a URL names, so https and ssh forms of one repo compare equal."""
    url = url.strip()
    if "://" in url:
        parts = urlsplit(url)
        host = "" if parts.scheme.lower() == "file" else (parts.hostname or "").lower()
        path = parts.path
    elif url.startswith("/"):
        host, path = "", url
    else:
        match = _SCP.fullmatch(url)
        if not match:
            return ("?", url)
        host, path = match["host"].lower(), match["path"]
    path = path.rstrip("/").removesuffix(".git").rstrip("/")
    return (host, path if not host else path.lstrip("/"))


def same_repo(a: str, b: str) -> bool:
    return _key(a) == _key(b)


def plan(remotes: dict[str, str] | None, repo: str, *, auto: bool = True) -> list[tuple[str, ...]]:
    """The git steps setup takes for ``remotes`` (None outside git); raises Refused, changing nothing."""
    if repo:
        validate(repo)
        if _is_upstream(repo):
            raise Refused("REPO= is GenieRails itself; pass the URL of your own (empty) repo. "
                          "GenieRails stays reachable as the genierails remote.")
        if remotes is None:
            return [("init",), ("add", "origin", repo)]
        origin = remotes.get("origin")
        if origin is None:
            return [("add", "origin", repo)]
        if same_repo(origin, repo):
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


def _git_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(cwd), *args], text=True, capture_output=True, env=_git_env())
    except OSError as exc:
        raise Refused(f"git is not installed ({exc}); option A needs only the git binary.") from exc


def _git(cwd: Path, *args: str) -> None:
    proc = _run(cwd, *args)
    if proc.returncode:
        raise Refused(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")


def checkout(cloud_dir: Path) -> tuple[Path, dict[str, str] | None, str]:
    """(checkout root, its remotes or None outside git, why it's unusable or "")."""
    root = cloud_dir.resolve().parent
    proc = _run(cloud_dir, "rev-parse", "--show-toplevel")
    if proc.returncode:
        top = None
    else:
        top = Path(proc.stdout.strip()).resolve()
    if top is not None and top != root:
        return root, None, (f"{cloud_dir.resolve()} is inside the git repo at {top}, not a GenieRails checkout of "
                            f"its own (expected the repo root at {root}); setup won't change that repo's remotes. "
                            "Clone GenieRails on its own, or manage remotes yourself.")
    if not (root / "shared").is_dir():
        return root, None, (f"{root} doesn't hold GenieRails' shared/ next to {cloud_dir.name}/; "
                            "setup manages git only at a GenieRails checkout root.")
    if top is None:
        return root, None, ""
    remotes: dict[str, str] = {}
    for parts in (line.split() for line in _run(root, "remote", "-v").stdout.splitlines()):
        if len(parts) >= 2:
            remotes.setdefault(parts[0], parts[1])
    return root, remotes, ""


def apply(root: Path, steps: list[tuple[str, ...]]) -> list[str]:
    done = []
    for step in steps:
        if step[0] == "init":
            _git(root, "init", "-q")
            _git(root, "symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
            done.append(f"initialised a git repo in {root} (branch {BRANCH})")
        elif step[0] == "rename":
            _git(root, "remote", "rename", step[1], step[2])
            done.append(f"renamed remote {step[1]} -> {step[2]} (GenieRails, kept for upgrades)")
        else:
            _git(root, "remote", "add", step[1], step[2])
            done.append(f"added remote {step[1]} = {step[2]}")
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cloud-dir", required=True, type=Path)
    parser.add_argument("--gitignore", default="", help="make setup's GITIGNORE; off skips the no-REPO rename")
    parser.add_argument("--env", default="dev")
    parser.add_argument("--check", action="store_true", help="refuse or pass; change nothing")
    args = parser.parse_args(argv)
    repo = os.environ.get(REPO_ENV, "")
    try:
        root, remotes, unusable = checkout(args.cloud_dir)
        if unusable:
            if repo:
                raise Refused(unusable)
            return 0  # no REPO: nothing to do outside a GenieRails checkout root
        steps = plan(remotes, repo, auto=args.gitignore != "off" and not in_ci())
        if args.check:
            return 0
        done = apply(root, steps)
    except Refused as exc:
        print(f"ERROR: make setup REPO=: {exc}" if repo else f"ERROR: make setup: {exc}", file=sys.stderr)
        if repo:
            print(f"       {MANUAL}", file=sys.stderr)
        return 1
    for line in done:
        print(f"Git: {line}")
    if repo and not steps:
        print(f"Git: origin is already {remotes.get('origin')}; nothing to change.")
    elif done and not repo:
        print(f"Git: this is now a local-only repo (no origin): commit envs/ here; nothing is pushed. "
              f"Add your own repo later with make setup ENV={args.env} REPO=<url>. "
              f"Undo: git remote rename {UPSTREAM_REMOTE} origin")
    elif done:
        print(f"Git: nothing was pushed; the first push is git push -u origin {BRANCH}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
