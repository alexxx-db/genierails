#!/usr/bin/env python3
"""Secure helpers for envs/<env>/auth.auto.tfvars."""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path


AUTH_MODE = 0o600
_SECRET_RE = re.compile(
    r'(?m)^[ \t]*databricks_client_secret[ \t]*=[ \t]*"(?P<value>(?:[^"\\]|\\.)*)"[ \t]*(?:#.*)?$'
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _warn_tighten(path: Path, *, emit=print) -> None:
    mode = _mode(path)
    if mode != AUTH_MODE:
        emit(
            f"WARNING: {path} had mode {mode:04o}; tightening to 0600 because it may hold a client secret.",
            file=sys.stderr,
        )
        os.chmod(path, AUTH_MODE)


def ensure_template(path: Path, template: Path) -> None:
    if path.is_symlink():
        return
    if path.exists():
        _warn_tighten(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, AUTH_MODE)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(template.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _secret_value(text: str) -> str | None:
    match = _SECRET_RE.search(text)
    if not match:
        return None
    return match.group("value").strip()


def _can_overwrite(text: str) -> bool:
    value = _secret_value(text)
    return value in (None, "", "<newly-minted secret shown above>", "<existing-secret-not-retrievable>")


def render_auth_tfvars(
    *,
    account_id: str,
    client_id: str,
    client_secret: str,
    workspace_host: str,
    workspace_id: int,
) -> str:
    return (
        f'databricks_account_id    = "{account_id}"\n'
        f'databricks_client_id     = "{client_id}"\n'
        f'databricks_client_secret = "{client_secret}"\n'
        f'databricks_workspace_host = "{workspace_host}"\n'
        f'databricks_workspace_id   = "{workspace_id}"\n'
    )


def write_auth_tfvars(
    path: Path,
    *,
    content: str,
    force: bool = False,
) -> None:
    try:
        existing_stat = path.lstat()
    except FileNotFoundError:
        existing_stat = None
    if existing_stat is not None:
        if stat.S_ISLNK(existing_stat.st_mode):
            raise RuntimeError(f"{path} is a symlink; refusing to write secrets through it")
        if not stat.S_ISREG(existing_stat.st_mode):
            raise RuntimeError(f"{path} is not a regular file")
        _warn_tighten(path)
        if not force and not _can_overwrite(path.read_text()):
            raise RuntimeError(
                f"{path} already contains a client secret; pass --force to replace it"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(tmp, flags, AUTH_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, AUTH_MODE)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    ensure = sub.add_parser("ensure-template")
    ensure.add_argument("path", type=Path)
    ensure.add_argument("--template", required=True, type=Path)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "ensure-template":
        ensure_template(args.path, args.template)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
