#!/usr/bin/env python3
"""Persistent state layout, remote-backend validation, and safe migration."""
from __future__ import annotations
import argparse, os, shutil, sys
from pathlib import Path
import hcl2

def cloud_dir(env_dir): return Path(env_dir).resolve().parent.parent
def state_root(env_dir):
    value = os.environ.get("GENIERAILS_STATE_DIR", "").strip()
    return Path(value).expanduser().resolve() / cloud_dir(env_dir).name if value else None
def env_state(env_dir, env): return (state_root(env_dir) / env) if state_root(env_dir) else Path(env_dir).resolve()
def config(env_dir):
    path = Path(env_dir) / "remote_state.hcl"
    if not path.is_file(): return {}
    data = hcl2.loads(path.read_text())
    return data.get("remote_state", data)
def validate(cfg, cloud):
    backend, expected = cfg.get("backend", ""), ("s3" if cloud == "aws" else "azurerm")
    required = ({"bucket", "region", "dynamodb_table"} if backend == "s3" else
                {"resource_group_name", "storage_account_name", "container_name"} if backend == "azurerm" else set())
    missing = sorted(k for k in required if not str(cfg.get(k, "")).strip())
    if backend != expected: return False, f'remote_state.hcl must set backend = "{expected}" for {cloud}'
    if missing: return False, "remote state with locking is incomplete; missing: " + ", ".join(missing)
    return True, ""
def backend_args(env_dir, env, layer):
    cfg = config(env_dir)
    if not cfg: return []
    ok, reason = validate(cfg, cloud_dir(env_dir).name)
    if not ok: raise ValueError(reason)
    prefix = str(cfg.get("key_prefix", "genierails")).strip("/")
    values = {k: v for k, v in cfg.items() if k not in {"backend", "key_prefix"}}
    values["key"] = f"{prefix}/{env}/{layer}.tfstate"
    if cfg["backend"] == "s3": values.setdefault("encrypt", True)
    return [f"-backend-config={k}={str(v).lower() if isinstance(v, bool) else v}" for k, v in values.items()]
def prepare_root(env_dir, env, layer, source):
    cfg = config(env_dir)
    if not cfg: return source
    shared = env_state(env_dir, env) / "terraform-root" / "shared"
    root = shared / "roots" / layer; root.mkdir(parents=True, exist_ok=True); os.chmod(env_state(env_dir,env),0o700)
    for name in ("modules", "scripts"):
        link = shared / name
        if not link.exists(): link.symlink_to(source.parents[1] / name, target_is_directory=True)
    for item in source.iterdir():
        if item.is_file() and item.suffix == ".tf":
            text = item.read_text().replace('backend "local" {}', f'backend "{cfg["backend"]}" {{}}')
            (root / item.name).write_text(text)
    return root
def migrate(env_dir, env):
    env_dir, target = Path(env_dir), env_state(env_dir, env)
    if state_root(env_dir) is None: raise RuntimeError("set GENIERAILS_STATE_DIR before migrating")
    primary = "account" if env == "account" else "workspace"
    items = [(env_dir / n, primary) for n in ("terraform.tfstate", "terraform.tfstate.backup", ".terraform")]
    items += [(env_dir / "data_access" / n, "data_access") for n in ("terraform.tfstate", "terraform.tfstate.backup", ".terraform")]
    items += [(env_dir / "generated" / n, "locks") for n in (".governance.lock", ".governance.guard")]
    operations = []
    for src, layer in items:
        if not (src.exists() or src.is_symlink()): continue
        dst = target / ("locks" if layer == "locks" else f"layers/{layer}") / src.name
        operations.append((src, dst))
    legacy_records = cloud_dir(env_dir) / ".genierails-state" / env
    if legacy_records.is_dir():
        operations += [(src, target / src.relative_to(legacy_records))
                       for src in legacy_records.rglob("*") if src.is_file() or src.is_symlink()]
    conflicts = [dst for _, dst in operations if dst.exists() or dst.is_symlink()]
    if conflicts:
        raise RuntimeError("refusing to overwrite existing " + ", ".join(map(str, conflicts)))
    os.makedirs(target, mode=0o700, exist_ok=True); os.chmod(target, 0o700)
    moved = 0
    for src, dst in operations:
        dst.parent.mkdir(parents=True, exist_ok=True); shutil.move(str(src), str(dst)); moved += 1
        print(f"moved {src} -> {dst}")
    if legacy_records.is_dir():
        for directory in sorted((p for p in legacy_records.rglob("*") if p.is_dir()), reverse=True):
            directory.rmdir()
        legacy_records.rmdir()
    print(f"State-dir migration complete: {moved} item(s) moved; config and auth files were untouched.")
def main():
    p=argparse.ArgumentParser(); s=p.add_subparsers(dest="command", required=True)
    for name in ("status","ci-allowed","backend-args","root","lock-dir","migrate"):
        q=s.add_parser(name); q.add_argument("--env-dir",type=Path,required=True); q.add_argument("--env",required=True)
        if name in ("backend-args","root"): q.add_argument("--layer",required=True)
        if name == "root": q.add_argument("--source",type=Path,required=True)
    a=p.parse_args()
    try:
        if a.command=="status":
            cfg=config(a.env_dir)
            if not cfg:return 1
            ok,why=validate(cfg,cloud_dir(a.env_dir).name)
            if not ok:print(why,file=sys.stderr);return 2
            print("locked-remote");return 0
        if a.command=="ci-allowed":
            cfg=config(a.env_dir); ok,_=validate(cfg,cloud_dir(a.env_dir).name) if cfg else (False,"")
            if a.env != "dev" or not ok:return 1
            # The public exception is the outer target only. Recursive applying
            # makes pass only while they inherit the real dev rehearsal lock.
            goals=os.environ.get("GENIERAILS_MAKE_GOALS","").split()
            if goals == ["rehearse"]: return 0
            sys.path.insert(0,str(Path(__file__).resolve().parent))
            import environment_lock
            return 0 if environment_lock.held_by_ancestor(a.env_dir)=="rehearse" else 1
        if a.command=="backend-args": print("\n".join(backend_args(a.env_dir,a.env,a.layer)));return 0
        if a.command=="root": print(prepare_root(a.env_dir,a.env,a.layer,a.source));return 0
        if a.command=="lock-dir": print(env_state(a.env_dir,a.env)/"locks");return 0
        migrate(a.env_dir,a.env);return 0
    except Exception as exc: print(f"state layout: {exc}",file=sys.stderr);return 2
if __name__=="__main__": raise SystemExit(main())
