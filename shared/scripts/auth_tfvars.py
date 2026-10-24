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
    r'(?m)^[ \t]*databricks_client_secret[ \t]*=[ \t]*"(?P<value>(?:[^"\\\r\n]|\\.)*)"[ \t]*(?:#[^\r\n]*)?\r?$'
)
_SECRET_KEY_RE = re.compile(r"(?m)^[ \t]*databricks_client_secret[ \t]*=")
_ACCOUNT_HOST_RE = re.compile(r"(?m)^[ \t]*databricks_account_host[ \t]*=")
AZURE_ACCOUNT_HOST = "https://accounts.azuredatabricks.net"


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


def template_text(template: Path, cloud: str = "") -> bytes:
    """The template's bytes; Azure also gets its account host (the roots default to AWS's)."""
    data = template.read_bytes()
    if cloud == "azure" and not _ACCOUNT_HOST_RE.search(data.decode("utf-8")):
        data += f'databricks_account_host   = "{AZURE_ACCOUNT_HOST}"\n'.encode()
    return data


def ensure_template(path: Path, template: Path, cloud: str = "") -> None:
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
        os.fchmod(fd, AUTH_MODE)
        with os.fdopen(fd, "wb") as handle:
            handle.write(template_text(template, cloud))
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
    if value is None:
        return _SECRET_KEY_RE.search(text) is None
    return value in ("", "<newly-minted secret shown above>", "<existing-secret-not-retrievable>")


def preflight_auth_tfvars(path: Path, *, force: bool = False) -> None:
    """Fail closed before a secret is minted; the writer repeats these checks."""
    try:
        parent_stat = path.parent.lstat()
    except FileNotFoundError:
        parent_stat = None
    if parent_stat is not None and stat.S_ISLNK(parent_stat.st_mode):
        raise RuntimeError(f"{path.parent} is a symlink; refusing to write secrets through it")
    try:
        existing_stat = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(existing_stat.st_mode):
        raise RuntimeError(f"{path} is a symlink; refusing to write secrets through it")
    if not stat.S_ISREG(existing_stat.st_mode):
        raise RuntimeError(f"{path} is not a regular file")
    _warn_tighten(path)
    if not force and not _can_overwrite(path.read_text()):
        raise RuntimeError(
            f"{path} already contains a client secret or an unparseable secret assignment; "
            "pass --force to replace it"
        )


def render_auth_tfvars(
    *,
    account_id: str,
    client_id: str,
    client_secret: str,
    workspace_host: str,
    workspace_id: int,
    existing: str = "",
) -> str:
    values = {
        "databricks_account_id": (account_id, "databricks_account_id   "),
        "databricks_client_id": (client_id, "databricks_client_id    "),
        "databricks_client_secret": (client_secret, "databricks_client_secret"),
        "databricks_workspace_host": (workspace_host, "databricks_workspace_host"),
        "databricks_workspace_id": (str(workspace_id), "databricks_workspace_id  "),
    }
    text = existing
    for key, (value, padded_key) in values.items():
        replacement = f'{padded_key} = "{value}"'
        pattern = re.compile(
            rf'(?m)^(?P<prefix>[ \t]*{re.escape(key)}[ \t]*=[ \t]*)'
            rf'(?P<old>"(?:[^"\\\r\n]|\\.)*")(?P<suffix>[ \t]*(?:#[^\r\n]*)?\r?$)'
        )
        if pattern.search(text):
            text = pattern.sub(
                lambda match: f'{match.group("prefix")}"{value}"{match.group("suffix")}',
                text,
                count=1,
            )
        else:
            if text and not text.endswith(("\n", "\r")):
                text += "\n"
            text += replacement + "\n"
    return text


def write_auth_tfvars(
    path: Path,
    *,
    content: str,
    force: bool = False,
) -> None:
    preflight_auth_tfvars(path, force=force)
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
    ensure.add_argument("--cloud", default="", help="aws or azure; azure also writes databricks_account_host")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "ensure-template":
        ensure_template(args.path, args.template, args.cloud)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
