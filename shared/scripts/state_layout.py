#!/usr/bin/env python3
"""Where Terraform state and runtime files live, and the one way to read state.

Layouts (design section 15, rollout step 12b):

in-checkout   no GENIERAILS_STATE_DIR, no remote_state.hcl: state, TF data and
              the env lock stay under envs/<env>/ as before.
state dir     GENIERAILS_STATE_DIR set: <dir>/<cloud>/<env>/ holds
              layers/<layer>/{terraform.tfstate,.terraform}, locks/, receipts/,
              manifests/ and records/ (the policy journal and the other
              runtime records a fresh checkout must see, record_path); none of
              them is written under envs/<env>/.
remote        envs/<env>/remote_state.hcl: the layer's state is in S3 or Azure
              Blob (locked). Runtime files go to the state dir, or to
              <cloud>/.genierails-state/<env>/ when it is unset, never under
              envs/<env>/.

state_json is the only reader of Terraform state (tests enforce it): local
files are read where terraform_layer.sh writes them, remote state through
``terraform state pull``. A state that should exist but can't be read raises.

make migrate-state-dir moves the in-checkout layout (state and records) into
the state dir under the env lock, copy-verify-then-remove, and leaves
envs/<env>/.state-moved so a run without the variable refuses instead of
seeing empty state. With the variable set, a record still in the checkout
refuses too (record_path), so a half-moved env is never read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

STATE_DIR_ENV = "GENIERAILS_STATE_DIR"
CONFIG_NAME = "remote_state.hcl"
MOVED_MARKER = ".state-moved"
IN_CHECKOUT_STATE = ".genierails-state"
LAYERS = ("account", "data_access", "workspace")
# Non-secret backend settings only. Credentials come from the pipeline's OIDC
# identity; a key, password or token here would be committed and passed on
# Terraform's command line.
S3_KEYS = {
    "backend", "bucket", "region", "dynamodb_table", "use_lockfile", "key_prefix",
    "encrypt", "kms_key_id", "workspace_key_prefix",
}
AZURE_KEYS = {
    "backend", "resource_group_name", "storage_account_name", "container_name", "key_prefix",
    "use_azuread_auth", "use_oidc", "subscription_id", "tenant_id", "client_id", "snapshot",
}
# Never copied into a generated root.
ROOT_COPY_SKIP = ("docs", "tests", "examples", "logs", "__pycache__", ".pytest_cache", ".terraform", ".tmp")


class StateError(RuntimeError):
    pass


# -- where things live --------------------------------------------------------

def cloud_dir(env_dir: Path) -> Path:
    return Path(env_dir).resolve().parent.parent


def state_dir_setting() -> str:
    return os.environ.get(STATE_DIR_ENV, "").strip()


def state_root(env_dir: Path) -> Path | None:
    """GENIERAILS_STATE_DIR/<cloud>, or None when the variable is unset."""
    value = state_dir_setting()
    return Path(value).expanduser().resolve() / cloud_dir(env_dir).name if value else None


def config_path(env_dir: Path) -> Path:
    return Path(env_dir) / CONFIG_NAME


def remote_configured(env_dir: Path) -> bool:
    return config_path(env_dir).is_file()


def owner_dir(env_dir: Path, layer: str) -> Path:
    """The env dir whose config and state a layer uses (envs/account for the account layer)."""
    env_dir = Path(env_dir)
    if layer == "account" and env_dir.name != "account":
        return env_dir.parent / "account"
    return env_dir


def env_state(env_dir: Path, env: str) -> Path | None:
    """The env's runtime dir outside envs/, or None for the in-checkout layout."""
    root = state_root(env_dir)
    if root is not None:
        return root / env
    if remote_configured(env_dir):
        return cloud_dir(env_dir) / IN_CHECKOUT_STATE / env
    return None


def layer_base(env_dir: Path, env: str, layer: str) -> Path | None:
    """terraform_layer.sh's STATE_BASE (TF_DATA_DIR, local state file), or None in-checkout."""
    base = env_state(env_dir, env)
    return None if base is None else base / "layers" / layer


def legacy_state_path(env_dir: Path, layer: str) -> Path:
    """Where the in-checkout layout keeps a layer's state file."""
    env_dir = Path(env_dir)
    if layer == "data_access":
        return env_dir / "data_access" / "terraform.tfstate"
    return env_dir / "terraform.tfstate"


def refuse_moved(env_dir: Path) -> None:
    marker = Path(env_dir) / MOVED_MARKER
    if not state_dir_setting() and marker.exists():
        raise StateError(f"state was moved to GENIERAILS_STATE_DIR ({marker}); set it to the "
                         "migration destination before running")


# -- runtime records ----------------------------------------------------------

# Records under envs/<env>/ (env-relative) that a later run must find, even
# from a fresh checkout: the staged policy ledger (journal, generation
# snapshots, stage.json, the migration record with its "adopting" marker,
# verify results) and group_map's last applied mapping. With
# GENIERAILS_STATE_DIR set they live in <dir>/<cloud>/<env>/records/<same path>.
# Receipts, manifests, imports records and locks were already there; what
# each run re-derives (generated/.runtime/, the coverage check's files,
# .*.apply.sha) stays in the checkout.
CHECKOUT_RECORDS = (Path("data_access") / ".policy_generations", Path(".group_map_applied.json"))
RECORDS = "records"


class RecordError(StateError, ValueError):
    """A runtime record can't be located safely; nothing may read or write it."""


def _holds_files(path: Path) -> bool:
    return path.is_file() or (path.is_dir() and any(p.is_file() for p in path.rglob("*")))


def record_path(env_dir: Path, relpath: str | Path) -> Path:
    """Where the runtime record ``relpath`` (one of CHECKOUT_RECORDS, or under one) lives.

    Unset GENIERAILS_STATE_DIR: envs/<env>/<relpath>, unchanged (a moved-state
    marker refuses). Set: <dir>/<cloud>/<env>/records/<relpath>; refuses when
    the state dir exists but can't be read, and when the record, or any
    file of a record directory, is still in the checkout (only there, or in
    both places): run make migrate-state-dir, never silently start from nothing.
    """
    env_dir = Path(env_dir)
    relpath = Path(relpath)
    top = next((r for r in CHECKOUT_RECORDS if relpath == r or r in relpath.parents), None)
    if top is None:
        raise RecordError(f"{relpath} is not a runtime record")
    root = state_root(env_dir)
    if root is None:
        try:
            refuse_moved(env_dir)
        except StateError as exc:
            raise RecordError(str(exc)) from exc
        return env_dir / relpath
    base = Path(state_dir_setting()).expanduser()
    # A state dir not created yet holds no records (as it holds no state); one
    # that exists but can't be listed or read never reads as empty.
    for path in (base, root, root / env_dir.name, root / env_dir.name / RECORDS):
        if path.exists() and not (path.is_dir() and os.access(path, os.R_OK | os.X_OK)):
            raise RecordError(f"{STATE_DIR_ENV} is set but {path} can't be read; fix it before running")
    if _holds_files(env_dir / top):
        raise RecordError(f"{env_dir / top} is still in the checkout while {STATE_DIR_ENV} is set; run "
                          f"make migrate-state-dir ENV={env_dir.name} to move it into "
                          f"{root / env_dir.name / RECORDS}")
    return root / env_dir.name / RECORDS / relpath


def refuse_split(env_dir: Path, env: str, layer: str) -> None:
    """A state dir layer with no state while the old in-checkout file exists is a split, not empty."""
    if state_root(env_dir) is None or remote_configured(env_dir):
        return
    legacy = legacy_state_path(env_dir, layer)
    if legacy.is_file() and not (layer_base(env_dir, env, layer) / "terraform.tfstate").exists():
        raise StateError(f"{legacy} exists but GENIERAILS_STATE_DIR has no {layer} state; run "
                         f"make migrate-state-dir ENV={env} (or unset GENIERAILS_STATE_DIR)")


# -- remote_state.hcl ---------------------------------------------------------

def config(env_dir: Path) -> dict:
    path = config_path(env_dir)
    if not path.is_file():
        return {}
    import hcl2

    data = hcl2.loads(path.read_text()) or {}
    value = data.get("remote_state", data)
    if not isinstance(value, dict):
        raise StateError(f"{path} must contain an object")
    return value


def validate(cfg: dict, cloud: str) -> tuple[bool, str]:
    expected = "s3" if cloud == "aws" else "azurerm"
    backend = cfg.get("backend", "")
    if backend != expected:
        return False, f'remote_state.hcl must set backend = "{expected}" for {cloud}'
    unknown = sorted(set(cfg) - (S3_KEYS if backend == "s3" else AZURE_KEYS))
    if unknown:
        return False, ("remote_state.hcl contains forbidden/unknown keys (credentials never go here; "
                       "the pipeline's OIDC identity supplies them): " + ", ".join(unknown))
    if backend == "s3":
        missing = [k for k in ("bucket", "region") if not str(cfg.get(k, "")).strip()]
        if not str(cfg.get("dynamodb_table", "")).strip() and cfg.get("use_lockfile") is not True:
            missing.append("dynamodb_table or use_lockfile = true")
    else:
        missing = [k for k in ("resource_group_name", "storage_account_name", "container_name")
                   if not str(cfg.get(k, "")).strip()]
    if missing:
        return False, "remote state with locking is incomplete; missing: " + ", ".join(missing)
    return True, ""


def checked_config(env_dir: Path) -> dict:
    cfg = config(env_dir)
    if cfg:
        ok, reason = validate(cfg, cloud_dir(env_dir).name)
        if not ok:
            raise StateError(f"{config_path(env_dir)}: {reason}")
    return cfg


def backend_args(env_dir: Path, env: str, layer: str) -> list[str]:
    cfg = checked_config(env_dir)
    if not cfg:
        return []
    prefix = str(cfg.get("key_prefix", "genierails")).strip("/")
    values = {k: v for k, v in cfg.items() if k not in {"backend", "key_prefix"}}
    values["key"] = f"{prefix}/{env}/{layer}.tfstate"
    if cfg["backend"] == "s3":
        values.setdefault("encrypt", True)
    return [f"-backend-config={key}={str(value).lower() if isinstance(value, bool) else value}"
            for key, value in sorted(values.items())]


# -- generated roots ----------------------------------------------------------

def _copy_shared(shared: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    for item in shared.iterdir():
        if item.name in ROOT_COPY_SKIP:
            continue
        if item.is_dir():
            shutil.copytree(item, destination / item.name,
                            ignore=shutil.ignore_patterns(*ROOT_COPY_SKIP, "*.tfstate*", "*.tfplan"))
        elif item.is_file():
            shutil.copy2(item, destination / item.name)


def prepare_root(env_dir: Path, env: str, layer: str, source: Path) -> Path:
    """The Terraform root to run in: ``source`` itself, or a fresh copy whose backend is remote.

    A remote env needs ``backend "<s3|azurerm>" {}`` in place of the roots'
    ``backend "local" {}``. Each run copies the current checkout's shared/ (the
    root's path.root/../.. holds deploy_masking_functions.py, scripts/ and
    modules/), so a later commit never runs earlier code, a .tf deleted
    upstream is gone, and the provider lock file carries over. The copy is
    built aside and renamed into place, one per layer.
    """
    cfg = checked_config(env_dir)
    if not cfg:
        return Path(source)
    source = Path(source).resolve()
    base = env_state(env_dir, env) / "roots"
    base.mkdir(parents=True, exist_ok=True)
    os.chmod(base.parent, 0o700)
    current = base / layer
    tmp = Path(tempfile.mkdtemp(prefix=f".{layer}.", dir=base))
    try:
        _copy_shared(source.parents[1], tmp / "shared")
        root = tmp / "shared" / "roots" / layer
        replaced = False
        for tf_file in root.glob("*.tf"):
            text = tf_file.read_text()
            if 'backend "local" {}' in text:
                tf_file.write_text(text.replace('backend "local" {}', f'backend "{cfg["backend"]}" {{}}'))
                replaced = True
        if not replaced:
            raise StateError(f'{source} declares no backend "local" {{}} to replace')
        previous_lock = current / "shared" / "roots" / layer / ".terraform.lock.hcl"
        if not (root / ".terraform.lock.hcl").is_file() and previous_lock.is_file():
            shutil.copy2(previous_lock, root / ".terraform.lock.hcl")
        old = base / f".{layer}.old.{os.getpid()}"
        if current.exists():
            current.rename(old)
        tmp.rename(current)
        shutil.rmtree(old, ignore_errors=True)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return current / "shared" / "roots" / layer


# -- reading state ------------------------------------------------------------

def _pull(owner: Path, env: str, layer: str) -> dict | None:
    runner = Path(__file__).with_name("terraform_layer.sh")
    layer_env_dir = owner / "data_access" if layer == "data_access" else owner
    result = subprocess.run(
        [str(runner), layer, env, "state-pull"], text=True, capture_output=True, check=False,
        env=os.environ | {"LAYER_ENV_DIR": str(layer_env_dir), "ENVS_DIR": str(owner.parent)},
    )
    if result.returncode:
        raise StateError(f"terraform state pull failed: {(result.stderr or result.stdout).strip()[-2000:]}")
    text = result.stdout.strip()
    return json.loads(text) if text else None


def state_json(env_dir: Path, env: str, layer: str) -> dict | None:
    """A layer's Terraform state, read through its configured backend; None when there is none yet.

    Raises StateError when it should exist but can't be read: unreadable JSON,
    a failed remote pull, a moved-state marker without GENIERAILS_STATE_DIR,
    or a state dir missing a state the checkout still holds.
    """
    if layer not in LAYERS:
        raise StateError(f"unknown layer {layer}")
    owner = owner_dir(Path(env_dir).resolve(), layer)
    env = "account" if layer == "account" else env
    try:
        refuse_moved(owner)
        if remote_configured(owner):
            value = _pull(owner, env, layer)
        else:
            refuse_split(owner, env, layer)
            base = layer_base(owner, env, layer)
            path = base / "terraform.tfstate" if base else legacy_state_path(owner, layer)
            if not path.is_file():
                return None
            value = json.loads(path.read_text())
        if value is not None and not isinstance(value, dict):
            raise ValueError("state is not a JSON object")
        return value
    except StateError as exc:
        raise StateError(f"cannot read the {env} {layer} Terraform state: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise StateError(f"cannot read the {env} {layer} Terraform state (terraform.tfstate): {exc}") from exc


def write_state_file(env_dir: Path, env: str, layer: str, out: Path) -> bool:
    """Snapshot a layer's state (read through state_json) to ``out`` for a Terraform root to read.

    The workspace root reads its own and the data_access state with file()
    (CAN_RUN on record, the applied coverage check and grants); with a state
    dir or remote state, terraform_layer.sh passes it these snapshots. No
    state removes ``out`` (the root then sees none, as with no local file);
    an unreadable state raises, so the runner refuses rather than plan from
    nothing on record. Owner-only: state can hold secrets.
    """
    out = Path(out)
    state = state_json(env_dir, env, layer)
    if state is None:
        out.unlink(missing_ok=True)
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(out.parent, 0o700)
    tmp = out.with_name(f".{out.name}.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(state, handle)
    tmp.replace(out)
    return True


# Variables only terraform_layer.sh sets: where the roots read their state.
RUNNER_VARS = ("env_dir", "workspace_state_file", "data_access_state_file")


def _assigned_names(args: list[str], source: str) -> list[tuple[str, str]]:
    """(name, where) for every -var assignment and every -var-file in Terraform ``args``."""
    found, files = [], []
    i = 0
    while i < len(args):
        arg = args[i]
        flag, eq, value = arg.lstrip("-").partition("=")
        if arg.startswith("-") and flag in ("var", "var-file"):
            if not eq:  # the split form: -var name=value / -var-file path
                i += 1
                value = args[i] if i < len(args) else ""
            if flag == "var":
                found.append((value.split("=", 1)[0].strip(), f"{source}: {arg}{'' if eq else ' ' + value}"))
            else:
                files.append(value)
        i += 1
    for path in files:
        found += _file_names(Path(path), f"{source}: -var-file={path}")
    return found


def _file_names(path: Path, where: str) -> list[tuple[str, str]]:
    try:
        text = path.read_text()
        if path.suffix == ".json":
            data = json.loads(text)
        else:
            import hcl2

            data = hcl2.loads(text)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except Exception as exc:  # unreadable: can't tell what it sets, so it can't pass
        return [("<unreadable>", f"{where} ({type(exc).__name__}: {exc})")]
    return [(str(name), where) for name in data]


def runner_var_overrides(args: list[str], command: str, var_files: list[Path] = (),
                         environ: dict | None = None) -> list[str]:
    """Where a caller tries to set a variable only the runner may set (RUNNER_VARS).

    Covers -var name=value in both forms, -var-file contents (the caller's and
    the env's own tfvars), and TF_CLI_ARGS / TF_CLI_ARGS_<command>, which
    Terraform appends after every other argument. Names are case-sensitive,
    as in Terraform. A var file that can't be read counts as an override.
    """
    import shlex

    environ = os.environ if environ is None else environ
    found = _assigned_names(list(args), "arguments")
    for path in var_files:
        found += _file_names(Path(path), f"var file {path}")
    for name in ("TF_CLI_ARGS", f"TF_CLI_ARGS_{command}"):
        if environ.get(name, "").strip():
            found += _assigned_names(shlex.split(environ[name]), name)
    return [f"{where} sets {name}" if name != "<unreadable>" else f"{where} can't be read"
            for name, where in found if name in RUNNER_VARS or name == "<unreadable>"]


def root_state_paths(env_dir: Path) -> dict[str, Path]:
    """Each root's local state file (for tests and messages; a remote root has none)."""
    env_dir = Path(env_dir).resolve()
    paths = {}
    for layer in LAYERS:
        owner = owner_dir(env_dir, layer)
        env = "account" if layer == "account" else env_dir.name
        base = layer_base(owner, env, layer)
        paths[layer] = base / "terraform.tfstate" if base else legacy_state_path(owner, layer)
    return paths


# -- migration ----------------------------------------------------------------

def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _move_file(source: Path, destination: Path) -> str:
    """Copy, fsync, verify, rename into place, then remove the source; resumable after a crash.

    A destination that already holds the same bytes is a move a crash cut
    short, so only the source is removed. Different bytes refuse.
    """
    if destination.exists():
        if _digest(destination) != _digest(source):
            raise StateError(f"refusing to overwrite {destination}: it differs from {source}")
        source.unlink()
        return "already copied"
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".migrating")
    shutil.copy2(source, partial)
    with partial.open("rb") as handle:
        os.fsync(handle.fileno())
    if _digest(partial) != _digest(source):
        partial.unlink()
        raise StateError(f"verification failed while copying {source}")
    partial.rename(destination)
    _fsync_dir(destination.parent)
    source.unlink()
    _fsync_dir(source.parent)
    return "moved"


def _migrations(env_dir: Path, env: str, target: Path) -> list[tuple[Path, Path]]:
    layer = "account" if env == "account" else "workspace"
    moves = []
    for name in ("terraform.tfstate", "terraform.tfstate.backup"):
        moves.append((env_dir / name, target / "layers" / layer / name))
        if env != "account":
            moves.append((env_dir / "data_access" / name, target / "layers" / "data_access" / name))
    records = cloud_dir(env_dir) / IN_CHECKOUT_STATE / env
    if records.is_dir():
        for source in sorted(records.rglob("*")):
            rel = source.relative_to(records)
            # The lock stays (and is released) where it is; generated roots are rebuilt.
            if rel.parts[0] in ("locks", "roots") or not source.is_file() or source.name.endswith(".migrating"):
                continue
            moves.append((source, target / rel))
    for record in CHECKOUT_RECORDS:
        source = env_dir / record
        files = sorted(p for p in source.rglob("*") if p.is_file()) if source.is_dir() else [source]
        moves += [(f, target / RECORDS / f.relative_to(env_dir)) for f in files
                  if not f.name.endswith(".migrating")]
    return [(source, destination) for source, destination in moves if source.is_file()]


def _remove_empty_record_dirs(env_dir: Path) -> None:
    """After the move: the emptied record dirs go too, so no reader sees one left behind."""
    for record in CHECKOUT_RECORDS:
        source = env_dir / record
        if source.is_dir():
            for path in sorted((p for p in source.rglob("*") if p.is_dir()), reverse=True) + [source]:
                if not any(path.iterdir()):
                    path.rmdir()


def migrate(env_dir: Path, env: str) -> int:
    """Move the env's state and records into GENIERAILS_STATE_DIR; returns how many files moved."""
    env_dir = Path(env_dir).resolve()
    root = state_root(env_dir)
    if root is None:
        raise StateError("set GENIERAILS_STATE_DIR before migrating")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import environment_lock

    target = root / env
    pid = os.getpid()
    held = []
    try:
        # Both locks: a run without the variable takes the in-checkout one, a
        # run with it the state dir one. Either held means something is running.
        for state_dir in ("", state_dir_setting()):
            ok, message, _ = environment_lock.acquire(env_dir, pid, "migrate-state-dir", state_dir=state_dir)
            if not ok:
                raise StateError(f"{env} is locked, nothing was moved: {message}")
            held.append(state_dir)
        operations = _migrations(env_dir, env, target)
        conflicts = [str(d) for s, d in operations if d.exists() and _digest(d) != _digest(s)]
        if conflicts:
            raise StateError("refusing to overwrite existing " + ", ".join(conflicts))
        target.mkdir(parents=True, exist_ok=True)
        os.chmod(target, 0o700)
        # The marker goes first, durably (file and directory entry fsynced), so
        # from before any source is removed a run without the variable refuses.
        with (env_dir / MOVED_MARKER).open("w") as handle:
            handle.write(f"Terraform state and records moved to {target}.\n"
                         f"Set {STATE_DIR_ENV}={root.parent} before running make for this env.\n")
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_dir(env_dir)
        for source, destination in operations:
            print(f"{_move_file(source, destination)}: {source} -> {destination}")
        _remove_empty_record_dirs(env_dir)
        for cache in (env_dir / ".terraform", env_dir / "data_access" / ".terraform"):
            shutil.rmtree(cache, ignore_errors=True)
        print(f"State-dir migration complete: {len(operations)} file(s) moved to {target}; config and "
              f"auth files were untouched. {env_dir / MOVED_MARKER} now refuses runs without {STATE_DIR_ENV}.")
        return len(operations)
    finally:
        for state_dir in reversed(held):
            environment_lock.release_lock(env_dir, pid, state_dir=state_dir)


# -- the CI exception ---------------------------------------------------------

def ci_allowed(env_dir: Path, env: str, expected_cloud_dir: Path, cloud_root: Path | None = None) -> bool:
    """True only for make rehearse ENV=dev on <cloud>/envs/dev itself, with valid locked remote state.

    ENV_DIR must be that directory (no symlink, no other path resolving to
    it), CLOUD_ROOT must be the Makefile's own dir, dev must not be a
    promote-to destination, and envs/dev/remote_state.hcl, the one file
    terraform_layer.sh reads for it, must configure a locked backend. The outer
    goal must be exactly rehearse; recursive makes pass only while they
    inherit the dev rehearse lock.
    """
    if env != "dev":
        return False
    expected_cloud_dir = Path(expected_cloud_dir)
    canonical = expected_cloud_dir.resolve() / "envs" / "dev"
    if cloud_root is not None and Path(cloud_root).resolve() != expected_cloud_dir.resolve():
        return False
    given = Path(os.path.abspath(env_dir))
    if given != Path(os.path.abspath(expected_cloud_dir)) / "envs" / "dev":
        return False
    if given.is_symlink() or given.parent.is_symlink() or given.resolve() != canonical:
        return False
    try:
        cfg = checked_config(canonical)
    except Exception:
        return False
    if not cfg:
        return False
    env_cfg = canonical / "env.auto.tfvars"
    if env_cfg.is_file():
        import hcl2

        values = hcl2.loads(env_cfg.read_text()) or {}
        if values.get("promote_from") or values.get("env_role") == "promoted":
            return False
    if os.environ.get("GENIERAILS_MAKE_GOALS", "").split() == ["rehearse"]:
        return True
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import environment_lock

    return environment_lock.held_by_ancestor(canonical) == "rehearse"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check-runner-vars")
    check.add_argument("--tf-command", required=True)
    check.add_argument("--var-file", type=Path, action="append", default=[])
    check.add_argument("args", nargs=argparse.REMAINDER)
    for name in ("ci-allowed", "backend-args", "root", "layer-dir", "migrate", "state-json", "state-file"):
        command = commands.add_parser(name)
        command.add_argument("--env-dir", type=Path, required=True)
        command.add_argument("--env", required=True)
        if name == "ci-allowed":
            command.add_argument("--cloud-dir", type=Path, required=True)
            command.add_argument("--cloud-root", type=Path)
        if name in ("backend-args", "root", "layer-dir", "state-json", "state-file"):
            command.add_argument("--layer", required=True)
        if name == "state-file":
            command.add_argument("--out", type=Path, required=True)
        if name == "root":
            command.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "check-runner-vars":
            caller = args.args[1:] if args.args[:1] == ["--"] else args.args
            overrides = runner_var_overrides(caller, args.tf_command, args.var_file)
            for line in overrides:
                print(f"  {line}", file=sys.stderr)
            return 2 if overrides else 0
        if args.command == "ci-allowed":
            return 0 if ci_allowed(args.env_dir, args.env, args.cloud_dir, args.cloud_root) else 1
        if args.command == "backend-args":
            print("\n".join(backend_args(args.env_dir, args.env, args.layer)))
        elif args.command == "root":
            print(prepare_root(args.env_dir, args.env, args.layer, args.source))
        elif args.command == "layer-dir":
            refuse_moved(args.env_dir)
            refuse_split(args.env_dir, args.env, args.layer)
            print(layer_base(args.env_dir, args.env, args.layer) or "")
        elif args.command == "state-json":
            print(json.dumps(state_json(args.env_dir, args.env, args.layer)))
        elif args.command == "state-file":
            write_state_file(args.env_dir, args.env, args.layer, args.out)
        else:
            migrate(args.env_dir, args.env)
        return 0
    except Exception as exc:
        print(f"state layout: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
