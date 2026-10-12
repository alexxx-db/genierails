"""make setup's customer-repo .gitignore (design section 9, rollout step 15).

The GenieRails repo ignores envs/ everywhere; a customer repo commits it.
Only GITIGNORE=customer writes the managed block into <cloud>/.gitignore,
after which every setup refreshes it; the upstream repo never has the block,
so a plain setup there changes nothing. A real `git status --ignored` over a
sample legacy + deterministic env proves every runtime file is ignored and
every committed file is tracked.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SHARED = ROOT / "shared"
sys.path.insert(0, str(SHARED))

from scripts import customer_gitignore as cg  # noqa: E402
from scripts.release_gate import _is_input  # noqa: E402

CLOUDS = ("aws", "azure")
USER_LINES = "# my own ignores\n*.log\n!keep.log\n"


def _env():
    env = {k: v for k, v in os.environ.items() if k not in ("MAKEFLAGS", "MAKELEVEL", "ENV", "GITIGNORE")}
    env["PIP_NO_INDEX"] = "1"
    return env


def _setup(cloud, cloud_root, *args):
    return subprocess.run(
        ["make", "--no-print-directory", "setup", *args, f"CLOUD_ROOT={cloud_root}", f"SHARED_ROOT={SHARED}"],
        cwd=ROOT / cloud, text=True, capture_output=True, env=_env())


@pytest.mark.parametrize("cloud", CLOUDS)
def test_upstream_gitignore_has_no_customer_block(cloud):
    text = (ROOT / cloud / ".gitignore").read_text()
    assert cg.BEGIN not in text and cg.END not in text
    # ...and the repo root still ignores envs/, so upstream never tracks an env.
    root = (ROOT / ".gitignore").read_text().splitlines()
    assert {"envs/", "generated/", "masking_functions.sql"} <= set(root)


@pytest.mark.parametrize("cloud", CLOUDS)
def test_plain_setup_never_writes_the_gitignore(cloud, tmp_path):
    cloud_root = tmp_path / cloud
    cloud_root.mkdir()
    (cloud_root / ".gitignore").write_text(USER_LINES)
    result = _setup(cloud, cloud_root, "ENV=dev")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (cloud_root / ".gitignore").read_text() == USER_LINES
    assert f"make setup ENV=dev GITIGNORE=customer writes {cloud}/.gitignore" in result.stdout
    # No .gitignore at all: still nothing written.
    (cloud_root / ".gitignore").unlink()
    assert _setup(cloud, cloud_root, "ENV=dev").returncode == 0
    assert not (cloud_root / ".gitignore").exists()


@pytest.mark.parametrize("cloud", CLOUDS)
def test_customer_opt_in_is_sticky_idempotent_and_keeps_user_lines(cloud, tmp_path):
    cloud_root = tmp_path / cloud
    cloud_root.mkdir()
    gitignore = cloud_root / ".gitignore"
    gitignore.write_text(USER_LINES)

    first = _setup(cloud, cloud_root, "ENV=dev", "GITIGNORE=customer")
    assert first.returncode == 0, first.stdout + first.stderr
    text = gitignore.read_text()
    assert text.startswith(USER_LINES)
    assert text.count(cg.BEGIN) == 1 and text.endswith(cg.END + "\n")
    assert f"+++ b/{cloud}/.gitignore" in first.stdout and "+!/envs/" in first.stdout
    assert "/envs/dev/data_access/" not in text  # dev is legacy: its layer files are committed

    again = _setup(cloud, cloud_root, "ENV=dev", "GITIGNORE=customer")
    assert again.returncode == 0 and gitignore.read_text() == text
    assert f"{cloud}/.gitignore: up to date" in again.stdout and "+++" not in again.stdout

    # A user line added after the block survives a refresh, and a plain setup
    # (no GITIGNORE) refreshes the block once prod turns deterministic.
    gitignore.write_text(text + "my-notes.txt\n")
    assert _setup(cloud, cloud_root, "ENV=prod").returncode == 0
    env_file = cloud_root / "envs/prod/env.auto.tfvars"
    env_file.write_text(env_file.read_text() + '\ngovernance_mode = "deterministic"\n')
    refreshed = _setup(cloud, cloud_root, "ENV=prod")
    assert refreshed.returncode == 0, refreshed.stdout + refreshed.stderr
    new = gitignore.read_text()
    assert new.startswith(USER_LINES) and new.endswith("my-notes.txt\n")
    assert "+/envs/prod/data_access/" in refreshed.stdout
    assert "/envs/account/abac.auto.tfvars" not in new  # dev (legacy) still commits it

    assert _setup(cloud, cloud_root, "ENV=prod", "GITIGNORE=off").returncode == 0
    assert gitignore.read_text() == new


def test_damaged_markers_and_bad_values_refuse_without_writing(tmp_path, capsys):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(USER_LINES + cg.BEGIN + "\nenvs/\n")
    assert cg.main(["--cloud-dir", str(tmp_path), "--mode", "customer"]) == 1
    assert "markers are damaged" in capsys.readouterr().err
    assert gitignore.read_text() == USER_LINES + cg.BEGIN + "\nenvs/\n"
    assert cg.main(["--cloud-dir", str(tmp_path), "--mode", "yes"]) == 2
    assert "use customer, auto (the default) or off" in capsys.readouterr().err


# ── git status --ignored over a real sample env ──────────────────────────────

COMMITTED_ANY = [
    "env.auto.tfvars", "ack.txt", "agents/sales.auto.tfvars", "agents/sales.space.json",
    "generated/abac.auto.tfvars", "generated/masking_functions.sql", "generated/governed_tables.json",
    "generated/expected_classification.json", "generated/TUNING.md",
    "generated/spaces/sales/abac.auto.tfvars", "ddl/customers.sql",
]
LAYER_FILES = ["abac.auto.tfvars", "data_access/abac.auto.tfvars", "data_access/masking_functions.sql"]
RUNTIME_ANY = [
    "auth.auto.tfvars", "terraform.tfstate", "terraform.tfstate.backup", ".terraform.tfstate.lock.info",
    ".terraform/providers/registry.terraform.io/x", "workspace.tfplan",
    ".genie_space_id", ".genie_space_id_sales", ".genie_adopted_sales", ".genie_adopt_required_sales",
    ".workspace.apply.sha", ".can_run_check.json", ".agent_coverage.json", ".group_map_applied.json",
    "generated/.live_refresh.json", "generated/.governance.lock", "generated/.governance.guard",
    "generated/.runtime/abac.auto.tfvars", "generated/.verify_access.json",
    "generated/.verify_key_columns.saved.json", "generated/.history/1.json",
    "generated/.certified.json",
    "ddl/_fetched.sql", "classification.auto.tfvars", "discovered_uc_tables.auto.tfvars",
    "data_access/terraform.tfstate", "data_access/terraform.tfstate.backup",
    "data_access/.terraform/terraform.tfstate", "data_access/.data_access.apply.sha",
    "data_access/.coverage_gate.json", "data_access/.policy_generations/policies.json",
    "data_access/classification.auto.tfvars", "data_access/discovered_uc_tables.auto.tfvars",
    "__pycache__/x.pyc",
]
ACCOUNT_RUNTIME = ["terraform.tfstate", ".account.apply.sha", ".terraform/x"]


def _git(repo, *args):
    # The machine's own git config (global excludes, hooks) stays out of the sample repo.
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True, env=env).stdout


def _touch(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n")


def _status(repo):
    out = _git(repo, "status", "--porcelain", "--ignored", "--untracked-files=all")
    untracked = {line[3:] for line in out.splitlines() if line.startswith("?? ")}
    ignored = {line[3:].rstrip("/") for line in out.splitlines() if line.startswith("!! ")}
    return untracked, ignored


def _covered(path: str, ignored: set[str]) -> bool:
    return any(path == entry or path.startswith(entry + "/") for entry in ignored)


@pytest.mark.parametrize("cloud", CLOUDS)
@pytest.mark.parametrize("dev_mode", ["legacy", "deterministic"])
def test_git_status_ignores_every_runtime_file_and_tracks_every_committed_one(cloud, dev_mode, tmp_path):
    repo = tmp_path / "repo"
    cloud_root = repo / cloud
    cloud_root.mkdir(parents=True)
    # The customer repo starts as a copy of GenieRails: its real .gitignore files.
    (repo / ".gitignore").write_text((ROOT / ".gitignore").read_text())
    (cloud_root / ".gitignore").write_text((ROOT / cloud / ".gitignore").read_text())
    _git(repo, "init", "-q")
    envs = {"dev": dev_mode, "prod": "deterministic"}
    for env, mode in envs.items():
        assert _setup(cloud, cloud_root, f"ENV={env}").returncode == 0
        env_file = cloud_root / "envs" / env / "env.auto.tfvars"
        env_file.write_text(env_file.read_text() + f'\ngovernance_mode = "{mode}"\n')
    result = _setup(cloud, cloud_root, "ENV=prod", "GITIGNORE=customer")
    assert result.returncode == 0, result.stdout + result.stderr

    committed, runtime = [f"{cloud}/.gitignore"], []
    for env, mode in envs.items():
        base = f"{cloud}/envs/{env}"
        committed += [f"{base}/{f}" for f in COMMITTED_ANY]
        runtime += [f"{base}/{f}" for f in RUNTIME_ANY]
        (runtime if mode == "deterministic" else committed).extend(f"{base}/{f}" for f in LAYER_FILES)
        if mode == "deterministic":
            runtime.append(f"{base}/generated/genie_space_derived_acl_groups.auto.tfvars")
        # setup's symlinks: credentials and the data_access copy of env.auto.tfvars.
        runtime += [f"{base}/data_access/auth.auto.tfvars", f"{base}/data_access/env.auto.tfvars"]
    account = f"{cloud}/envs/account"
    committed.append(f"{account}/env.auto.tfvars")
    (runtime if dev_mode == "deterministic" else committed).append(f"{account}/abac.auto.tfvars")
    runtime += [f"{account}/{f}" for f in ACCOUNT_RUNTIME] + [f"{account}/auth.auto.tfvars"]
    runtime += [f"{cloud}/.genierails-state/dev/receipts/abc.json"]
    for rel in committed + runtime:
        if not (repo / rel).is_symlink() and not (repo / rel).exists():
            _touch(repo / rel)

    untracked, ignored = _status(repo)
    tracked_files = {p for p in untracked if p.startswith(f"{cloud}/")}
    assert tracked_files == set(committed), (
        f"not committable: {sorted(set(committed) - tracked_files)}; "
        f"leaked runtime files: {sorted(tracked_files - set(committed))}")
    assert all(_covered(rel, ignored) for rel in runtime), sorted(r for r in runtime if not _covered(r, ignored))

    # The deterministic env agrees exactly with release_gate's definition of a
    # committed input (what rehearse fingerprints): ignored iff not an input.
    prod = cloud_root / "envs/prod"
    for path in sorted(prod.rglob("*")):
        if path.is_file() and not path.is_symlink():
            rel = path.relative_to(repo).as_posix()
            assert _covered(rel, ignored) != _is_input(path.relative_to(prod)), rel

    # git add -A (what a user would run) stages no runtime file.
    _git(repo, "add", "-A")
    staged = set(_git(repo, "diff", "--cached", "--name-only").splitlines())
    assert not staged & set(runtime)
