"""No side door around release gating (rollout step 9).

In a deterministic env, every make target that applies the data_access or
workspace root runs only under the env lock of the command allowed to run it
there (scripts/release_gate.py apply-guard): a promoted env changes only
through release MANIFEST= (saved plans) and maintain's governance apply; the
rehearse env only through rehearse (and ungovern's governance apply). Real
make; recursive makes, Terraform and the controller are recording stubs. Each
test fails if its gate is removed: the stubs would then record the apply.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import environment_lock

ROOT = Path(__file__).resolve().parents[2]
DET = 'governance_mode = "deterministic"\naccess_tier_groups = ["analysts", "viewers"]\n'
PROMOTED = DET + 'promote_from = "dev"\ncatalog_map = { "dev_cat" = "prod_cat" }\n'
LEGACY = 'promote_from = "dev"\n'
STUB = f"#!{sys.executable}\nimport json, os, sys\n" \
       "open(os.environ['SIDE_DOOR_LOG'], 'a').write(json.dumps([os.path.basename(sys.argv[0])] + sys.argv[1:]) + '\\n')\n"


@pytest.fixture
def envs(tmp_path):
    cloud = tmp_path / "aws"
    for name, text in (("account", "manage_groups = false\n"), ("dev", DET), ("prod", PROMOTED),
                       ("stg", PROMOTED), ("legacy", LEGACY)):
        env = cloud / "envs" / name
        (env / "generated").mkdir(parents=True)
        (env / "data_access").mkdir()
        (env / "env.auto.tfvars").write_text(text)
        (env / "abac.auto.tfvars").write_text("# workspace\n")
        (env / "data_access" / "abac.auto.tfvars").write_text("fgac_policies = []\n")
    stubs = {}
    for name in ("make", "runner", "staged", "gate", "sticky", "agent", "importer"):
        path = tmp_path / name
        path.write_text(STUB)
        path.chmod(0o755)
        stubs[name] = path
    return cloud, stubs


def run(tmp_path, envs, target, env, *extra, stub_make=True):
    cloud, stubs = envs
    env_dir = cloud / "envs" / env
    log = tmp_path / "calls.jsonl"
    log.unlink(missing_ok=True)
    args = ["make", "--no-print-directory", target, f"ENV={env}", f"ENV_DIR={env_dir}",
            f"ACCOUNT_ENV_DIR={cloud / 'envs' / 'account'}", f"ROOT_RUNNER={stubs['runner']}",
            f"_STAGED_POLICY={stubs['staged']}", f"_COVERAGE_GATE={stubs['gate']}", f"_STICKY={stubs['sticky']}",
            f"_AGENT_COVERAGE={stubs['agent']}", f"IMPORT_EXISTING_SCRIPT={stubs['importer']}",
            "_ACCESS_CHECK=true", "_GROUP_CHECK=true", "_DEPLOYER_CHECK=true", *extra]
    if stub_make:
        args.append(f"MAKE={stubs['make']}")
    environ = {k: v for k, v in os.environ.items() if k not in ("MAKEFLAGS", "MAKELEVEL", "CI", "APPLY_FLAGS")}
    result = subprocess.run(args, cwd=ROOT / "aws", text=True, capture_output=True,
                            env=environ | {"SIDE_DOOR_LOG": str(log)})
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, calls


def applied(calls) -> list:
    """Calls that would apply something (anything past the bootstrap prerequisites)."""
    boot = {"_guard-root", "_prepare-env", "_guard-genie-placeholder"}
    return [c for c in calls if not (c[0] == "make" and boot & set(c))]


@pytest.fixture
def lock(envs, monkeypatch):
    """Hold an env's lock as make <owner> does: this process (an ancestor of make) takes it
    and exports its nonce to its children, as _ENV_LOCK does."""
    cloud, _ = envs
    held = []

    def take(env: str, owner: str):
        ok, message, nonce = environment_lock.acquire(cloud / "envs" / env, os.getpid(), owner)
        assert ok, message
        monkeypatch.setenv(environment_lock.nonce_var(cloud / "envs" / env), nonce)
        held.append(cloud / "envs" / env)

    yield take
    for env_dir in held:
        environment_lock.release_lock(env_dir, os.getpid())


# -- promoted envs: only release MANIFEST= and maintain --------------------------------

@pytest.mark.parametrize("env", ["prod", "stg"])
@pytest.mark.parametrize("target", ["apply", "apply-governance", "apply-genie"])
def test_a_promoted_env_refuses_direct_applies(tmp_path, envs, target, env):
    result, calls = run(tmp_path, envs, target, env)
    assert result.returncode != 0
    assert f"make {target} refused" in result.stderr
    assert f"make release-plan ENV={env}" in result.stderr and "MANIFEST=" in result.stderr
    assert applied(calls) == []


@pytest.mark.parametrize("layer", ["data_access", "workspace"])
def test_a_promoted_env_refuses_a_direct_layer_apply(tmp_path, envs, layer):
    cloud, _ = envs
    layer_dir = cloud / "envs" / "prod" / ("data_access" if layer == "data_access" else "")
    result, calls = run(tmp_path, envs, "_apply-layer", "prod", f"LAYER={layer}", "TARGET_ENV=prod",
                        f"LAYER_ENV_DIR={layer_dir}", "FORCE_APPLY=1")
    assert result.returncode != 0 and f"(the {layer} layer) refused" in result.stderr
    assert calls == []


@pytest.mark.parametrize("owner", ["release", "release-plan", "rehearse", "ungovern", "apply-account"])
def test_no_other_lock_owner_opens_a_promoted_env(tmp_path, envs, lock, owner):
    lock("prod", owner)
    for target in ("apply", "apply-governance"):
        result, calls = run(tmp_path, envs, target, "prod")
        assert result.returncode != 0 and f"(it runs under make {owner})" in result.stderr, target
        assert applied(calls) == []


def test_maintain_may_reapply_governance_but_never_the_workspace(tmp_path, envs, lock):
    lock("prod", "maintain")
    result, calls = run(tmp_path, envs, "apply-governance", "prod")
    assert result.returncode == 0, result.stdout + result.stderr
    assert any("_apply-layer" in c for c in calls)
    cloud, _ = envs
    result, calls = run(tmp_path, envs, "_apply-layer", "prod", "LAYER=data_access", "TARGET_ENV=prod",
                        f"LAYER_ENV_DIR={cloud / 'envs' / 'prod' / 'data_access'}", "FORCE_APPLY=1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(c[0] == "staged" and c[1] == "run" for c in calls)
    for target in ("apply", "apply-genie"):
        result, calls = run(tmp_path, envs, target, "prod")
        assert result.returncode != 0 and applied(calls) == [], target
    result, calls = run(tmp_path, envs, "_apply-layer", "prod", "LAYER=workspace", "TARGET_ENV=prod",
                        f"LAYER_ENV_DIR={cloud / 'envs' / 'prod'}")
    assert result.returncode != 0 and calls == []


def test_a_promoted_env_is_never_rehearsed(tmp_path, envs):
    result, calls = run(tmp_path, envs, "rehearse", "stg")
    assert result.returncode != 0 and "make release-plan ENV=stg" in result.stderr
    assert calls == []


def test_destroying_a_promoted_env_needs_confirmation(tmp_path, envs):
    result, calls = run(tmp_path, envs, "destroy", "prod", stub_make=False)
    assert result.returncode != 0 and "CONFIRM_DESTROY=prod" in result.stderr
    assert not [c for c in calls if c[0] == "runner"]
    result, calls = run(tmp_path, envs, "destroy", "prod", "CONFIRM_DESTROY=stg", stub_make=False)
    assert result.returncode != 0 and not [c for c in calls if c[0] == "runner"]
    result, calls = run(tmp_path, envs, "destroy", "prod", "CONFIRM_DESTROY=prod", stub_make=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [c[1:4] for c in calls if c[0] == "runner"] == [["workspace", "prod", "destroy"],
                                                         ["data_access", "prod", "destroy"]]


def test_confirm_destroy_counts_only_on_the_command_line(tmp_path, envs, monkeypatch):
    monkeypatch.setenv("CONFIRM_DESTROY", "prod")
    result, calls = run(tmp_path, envs, "destroy-governance", "prod", stub_make=False)
    assert result.returncode != 0 and not [c for c in calls if c[0] == "runner"]


def test_ungovern_in_a_promoted_env_edits_and_applies_nothing(tmp_path, envs):
    result, calls = run(tmp_path, envs, "ungovern", "prod", "TABLE=prod_cat.sch.t", "YES=1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert ["sticky", "ungovern", "--env-dir", str(envs[0] / "envs" / "prod"), "--table", "prod_cat.sch.t",
            "--commit"] in calls
    assert not [c for c in calls if c[0] == "make" and "apply-governance" in c]
    assert "make release-plan ENV=prod" in result.stdout


# -- the rehearse env: only through rehearse -------------------------------------------

@pytest.mark.parametrize("target", ["apply", "apply-governance", "apply-genie"])
def test_the_rehearse_env_refuses_direct_applies(tmp_path, envs, target):
    result, calls = run(tmp_path, envs, target, "dev")
    assert result.returncode != 0
    assert f"make {target} refused" in result.stderr and "make rehearse ENV=dev" in result.stderr
    assert applied(calls) == []


@pytest.mark.parametrize("layer", ["data_access", "workspace"])
def test_the_rehearse_env_refuses_a_direct_layer_apply(tmp_path, envs, layer):
    cloud, _ = envs
    layer_dir = cloud / "envs" / "dev" / ("data_access" if layer == "data_access" else "")
    result, calls = run(tmp_path, envs, "_apply-layer", "dev", f"LAYER={layer}", "TARGET_ENV=dev",
                        f"LAYER_ENV_DIR={layer_dir}", "FORCE_APPLY=1")
    assert result.returncode != 0 and "make rehearse ENV=dev" in result.stderr
    assert calls == []


@pytest.mark.parametrize("target", ["apply", "apply-governance", "apply-genie"])
def test_rehearse_applies_the_rehearse_env(tmp_path, envs, lock, target):
    lock("dev", "rehearse")
    result, calls = run(tmp_path, envs, target, "dev")
    assert result.returncode == 0, result.stdout + result.stderr
    assert any("_apply-layer" in c for c in calls)


def test_ungovern_applies_the_rehearse_env_governance_only(tmp_path, envs, lock):
    lock("dev", "ungovern")
    assert run(tmp_path, envs, "apply-governance", "dev")[0].returncode == 0
    result, calls = run(tmp_path, envs, "apply", "dev")
    assert result.returncode != 0 and applied(calls) == []


def test_another_process_holding_the_lock_does_not_open_the_gate(tmp_path, envs, monkeypatch):
    """The lock must be held by an ancestor: an unrelated live holder is not a pass, even with
    its nonce leaked into this environment (say, exported in a shell)."""
    cloud, _ = envs
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        ok, message, nonce = environment_lock.acquire(cloud / "envs" / "dev", other.pid, "rehearse")
        assert ok, message
        monkeypatch.setenv(environment_lock.nonce_var(cloud / "envs" / "dev"), nonce)
        result, calls = run(tmp_path, envs, "apply", "dev")
        assert result.returncode != 0 and applied(calls) == []
    finally:
        environment_lock.release_lock(cloud / "envs" / "dev", other.pid)
        other.kill()


# -- legacy envs: unchanged --------------------------------------------------------------

@pytest.mark.parametrize("target", ["apply", "apply-governance", "apply-genie"])
def test_legacy_envs_are_never_gated(tmp_path, envs, target):
    result, calls = run(tmp_path, envs, target, "legacy")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "refused" not in result.stderr
    assert any("_apply-layer" in c for c in calls)


def test_legacy_destroy_needs_no_confirmation(tmp_path, envs):
    result, calls = run(tmp_path, envs, "destroy", "legacy", stub_make=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [c[1] for c in calls if c[0] == "runner"] == ["workspace", "data_access"]


# -- the lock must be the one the enclosing gated command took ---------------------------
# A hand-written, copied or leftover lock file is never a pass, even when its pid is an
# ancestor (threat model: stale files and accidental misuse; a deliberate forger on the
# deployment machine could run Terraform directly).

def _write_lock(env_dir: Path, **fields) -> None:
    import socket

    record = {"pid": os.getpid(), "host": socket.gethostname(), "owner": "maintain"} | fields
    (env_dir / environment_lock.LOCK_RELPATH).write_text(json.dumps(record))


def test_a_forged_ancestor_lock_without_a_nonce_is_refused(tmp_path, envs):
    cloud, _ = envs
    _write_lock(cloud / "envs" / "prod")  # pid: this process, an ancestor of make
    for target in ("apply-governance",):
        result, calls = run(tmp_path, envs, target, "prod")
        assert result.returncode != 0 and "make apply-governance refused" in result.stderr
        assert applied(calls) == []
    result, calls = run(tmp_path, envs, "_apply-layer", "prod", "LAYER=data_access", "TARGET_ENV=prod",
                        f"LAYER_ENV_DIR={cloud / 'envs' / 'prod' / 'data_access'}", "FORCE_APPLY=1")
    assert result.returncode != 0 and calls == []


def test_a_matching_nonce_on_a_reused_pid_is_refused(tmp_path, envs, monkeypatch):
    cloud, _ = envs
    env_dir = cloud / "envs" / "prod"
    # A stale file whose pid now belongs to another process (this one): its start time differs.
    _write_lock(env_dir, nonce="n" * 64, pid_start="Mon Jan  1 00:00:00 2001")
    monkeypatch.setenv(environment_lock.nonce_var(env_dir), "n" * 64)
    result, calls = run(tmp_path, envs, "apply-governance", "prod")
    assert result.returncode != 0 and applied(calls) == []


def test_the_nonce_of_another_run_is_refused(tmp_path, envs, monkeypatch):
    cloud, _ = envs
    env_dir = cloud / "envs" / "prod"
    _write_lock(env_dir, nonce="a" * 64, pid_start=environment_lock.process_start(os.getpid()))
    monkeypatch.setenv(environment_lock.nonce_var(env_dir), "b" * 64)
    result, calls = run(tmp_path, envs, "apply-governance", "prod")
    assert result.returncode != 0 and applied(calls) == []
    # The same file with the right nonce is the enclosing run's own lock.
    monkeypatch.setenv(environment_lock.nonce_var(env_dir), "a" * 64)
    result, calls = run(tmp_path, envs, "apply-governance", "prod")
    assert result.returncode == 0, result.stdout + result.stderr


def test_an_inherited_nonce_without_a_lock_file_is_refused(tmp_path, envs, monkeypatch):
    cloud, _ = envs
    monkeypatch.setenv(environment_lock.nonce_var(cloud / "envs" / "prod"), "c" * 64)
    result, calls = run(tmp_path, envs, "apply-governance", "prod")
    assert result.returncode != 0 and applied(calls) == []


def test_lock_files_are_owner_only(tmp_path):
    old = os.umask(0)
    try:
        ok, _message = environment_lock.acquire_lock(tmp_path, os.getpid(), "maintain")
    finally:
        os.umask(old)
    assert ok
    path = tmp_path / environment_lock.LOCK_RELPATH
    assert path.stat().st_mode & 0o777 == 0o600
    record = json.loads(path.read_text())
    assert len(record["nonce"]) == 64 and record["pid_start"] == environment_lock.process_start(os.getpid())
    environment_lock.release_lock(tmp_path, os.getpid())
