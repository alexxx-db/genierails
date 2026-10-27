"""make setup REPO=<url>: GenieRails prepares the customer git repo (option A).

A fresh GenieRails clone (origin at GenieRails) run with REPO= keeps
GenieRails as the ``genierails`` remote and makes origin the customer's repo;
re-running with the same REPO changes nothing, and a different REPO once
origin exists is refused before anything is written (no env file, no remote
change). Without REPO a fresh clone becomes a local-only repo (no origin), so
only the git binary is needed. Nothing is ever pushed: REPO is a local bare
repo here and must stay empty. Real make setup runs in temporary repos, with
GIT_CONFIG_GLOBAL=/dev/null so the developer's global hooks and signing don't
apply.
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
from scripts import setup_repo as sr  # noqa: E402

UPSTREAM = "https://github.com/databricks-solutions/genierails.git"
CLOUDS = ("aws", "azure")


def _env(**extra):
    drop = ("MAKEFLAGS", "MAKELEVEL", "ENV", "GITIGNORE", "REPO", "CI", "GENIERAILS_ALLOW_CI_APPLY",
            "TF_BUILD", "JENKINS_URL", "GITLAB_CI", "BUILDKITE", "CIRCLECI")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(PIP_NO_INDEX="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", **extra)
    return env


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True,
                          env=_env(), check=True).stdout


def _remotes(repo):
    return dict(line.split()[:2] for line in _git(repo, "remote", "-v").splitlines() if "(fetch)" in line)


def _clone(tmp_path, cloud="aws", origin=UPSTREAM):
    """A checkout shaped like a fresh git clone of GenieRails (no network)."""
    repo = tmp_path / "genierails"
    (repo / cloud).mkdir(parents=True)
    _git(repo, "init", "-q")
    if origin:
        _git(repo, "remote", "add", "origin", origin)
    return repo


def _customer_repo(tmp_path, name="gov.git"):
    bare = tmp_path / name
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, env=_env())
    return str(bare)


def _setup(cloud_root, *args, env=None):
    return subprocess.run(
        ["make", "--no-print-directory", "setup", *args, f"CLOUD_ROOT={cloud_root}", f"SHARED_ROOT={SHARED}"],
        cwd=ROOT / cloud_root.name, text=True, capture_output=True, env=env or _env())


def _snapshot(repo):
    """Every file under repo (outside .git) with its bytes, plus the git config."""
    files = {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*")
             if p.is_file() and ".git" not in p.relative_to(repo).parts}
    return files, (repo / ".git" / "config").read_bytes()


def _no_refs(bare):
    return _git(bare, "for-each-ref") == ""


@pytest.mark.parametrize("cloud", CLOUDS)
def test_fresh_clone_with_repo_renames_origin_adds_repo_and_pushes_nothing(cloud, tmp_path):
    repo = _clone(tmp_path, cloud)
    url = _customer_repo(tmp_path)
    result = _setup(repo / cloud, "ENV=dev", f"REPO={url}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _remotes(repo) == {"genierails": UPSTREAM, "origin": url}
    assert "Git: renamed remote origin -> genierails" in result.stdout
    assert f"Git: added remote origin = {url}" in result.stdout
    assert "nothing was pushed" in result.stdout
    assert _no_refs(url)
    # origin is now the customer's, so the same setup run wrote the .gitignore block.
    assert cg.BEGIN in (repo / cloud / ".gitignore").read_text()
    assert (repo / cloud / "envs/dev/env.auto.tfvars").is_file()


def test_rerun_with_the_same_repo_is_a_no_op(tmp_path):
    repo = _clone(tmp_path)
    url = _customer_repo(tmp_path)
    assert _setup(repo / "aws", "ENV=dev", f"REPO={url}").returncode == 0
    before = _snapshot(repo)
    for same in (url, url + "/", url.removesuffix(".git")):
        again = _setup(repo / "aws", "ENV=dev", f"REPO={same}")
        assert again.returncode == 0, again.stdout + again.stderr
        assert "nothing to change" in again.stdout and "renamed" not in again.stdout
        assert _snapshot(repo) == before
    # No REPO at all on a customer repo: remotes untouched, nothing printed about git.
    plain = _setup(repo / "aws", "ENV=dev")
    assert plain.returncode == 0 and "Git:" not in plain.stdout
    assert _snapshot(repo) == before
    assert _no_refs(url)


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_conflicting_repo_is_refused_before_anything_is_written(env, tmp_path):
    repo = _clone(tmp_path)
    url = _customer_repo(tmp_path)
    assert _setup(repo / "aws", "ENV=dev", f"REPO={url}").returncode == 0
    before = _snapshot(repo)
    other = _customer_repo(tmp_path, "other.git")
    result = _setup(repo / "aws", f"ENV={env}", f"REPO={other}")
    assert result.returncode != 0
    assert f"origin already points at {url}, not {other}" in result.stderr
    assert "never repoints" in result.stderr and f"git remote set-url origin {other}" in result.stderr
    assert "leave REPO= off" in result.stderr.replace("Leave", "leave")
    assert _snapshot(repo) == before  # prod's env dir was not created, the .gitignore is unchanged
    assert not (repo / "aws/envs/prod").exists()
    assert "=== Setup" not in result.stdout


def test_refused_repo_on_a_fresh_clone_writes_nothing(tmp_path):
    repo = _clone(tmp_path)
    _git(repo, "remote", "add", "genierails", "https://example.com/mirror.git")
    before = _snapshot(repo)
    result = _setup(repo / "aws", "ENV=dev", f"REPO={_customer_repo(tmp_path)}")
    assert result.returncode != 0
    assert "a genierails remote already exists" in result.stderr
    assert _snapshot(repo) == before and not (repo / "aws/envs").exists()


@pytest.mark.parametrize("bad", [UPSTREAM, "git@github.com:databricks-solutions/genierails.git", "-c x", "a b"])
def test_repo_must_be_your_own_url(bad, tmp_path):
    repo = _clone(tmp_path)
    before = _snapshot(repo)
    result = _setup(repo / "aws", "ENV=dev", f"REPO={bad}")
    assert result.returncode != 0 and "ERROR: make setup REPO=" in result.stderr
    assert _snapshot(repo) == before and not (repo / "aws/envs").exists()


def test_the_scheduled_governance_guard_still_runs_first_and_no_remote_changes(tmp_path):
    repo = _clone(tmp_path)
    dev = repo / "aws/envs/dev"
    dev.mkdir(parents=True)
    (dev / "env.auto.tfvars").write_text('governance_mode = "deterministic"\nenable_scheduled_governance = true\n')
    before = _snapshot(repo)
    result = _setup(repo / "aws", "ENV=dev", f"REPO={_customer_repo(tmp_path)}")
    assert result.returncode != 0
    assert "enable_scheduled_governance = true is not supported" in result.stderr
    assert _snapshot(repo) == before and _remotes(repo) == {"origin": UPSTREAM}


def test_no_repo_makes_a_fresh_clone_local_only(tmp_path):
    repo = _clone(tmp_path)
    result = _setup(repo / "aws", "ENV=dev")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _remotes(repo) == {"genierails": UPSTREAM}
    assert "local-only repo" in result.stdout and "make setup ENV=dev REPO=<url>" in result.stdout
    assert cg.BEGIN in (repo / "aws/.gitignore").read_text()
    before = _snapshot(repo)
    again = _setup(repo / "aws", "ENV=dev")
    assert again.returncode == 0 and "Git:" not in again.stdout and _snapshot(repo) == before
    # Adding the customer repo later only adds origin.
    url = _customer_repo(tmp_path)
    later = _setup(repo / "aws", "ENV=dev", f"REPO={url}")
    assert later.returncode == 0, later.stdout + later.stderr
    assert _remotes(repo) == {"genierails": UPSTREAM, "origin": url} and "renamed" not in later.stdout
    assert _no_refs(url)


@pytest.mark.parametrize("opt_out", [("GITIGNORE=off",), ()])
def test_no_repo_leaves_remotes_alone_for_contributors_and_ci(opt_out, tmp_path):
    repo = _clone(tmp_path)
    env = None if opt_out else _env(CI="true")
    result = _setup(repo / "aws", "ENV=dev", *opt_out, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _remotes(repo) == {"origin": UPSTREAM} and "Git:" not in result.stdout


def test_an_exported_repo_is_ignored(tmp_path):
    """Only a command-line REPO counts (bootstrap-pipelines' REPO=<owner>/<name> may be exported)."""
    repo = _clone(tmp_path)
    result = _setup(repo / "aws", "ENV=dev", "GITIGNORE=off", env=_env(REPO="acme/gov"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert _remotes(repo) == {"origin": UPSTREAM}


def test_outside_git_repo_initialises_a_repo_on_main(tmp_path):
    root = tmp_path / "genierails"
    (root / "aws").mkdir(parents=True)
    url = _customer_repo(tmp_path)
    result = _setup(root / "aws", "ENV=dev", f"REPO={url}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _remotes(root) == {"origin": url}
    assert _git(root, "symbolic-ref", "HEAD").strip() == "refs/heads/main"
    assert cg.BEGIN in (root / "aws/.gitignore").read_text()
    assert _no_refs(url)


def test_outside_git_without_repo_still_only_prints_the_hint(tmp_path):
    root = tmp_path / "genierails"
    (root / "aws").mkdir(parents=True)
    result = _setup(root / "aws", "ENV=dev")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (root / ".git").exists() and "Git:" not in result.stdout


def test_plan_layouts():
    url = "git@example.com:acme/gov.git"
    assert sr.plan({"origin": UPSTREAM}, url) == [("rename", "origin", "genierails"), ("add", "origin", url)]
    assert sr.plan({"genierails": UPSTREAM}, url) == [("add", "origin", url)]
    assert sr.plan({}, url) == [("add", "origin", url)]
    assert sr.plan(None, url) == [("init",), ("add", "origin", url)]
    assert sr.plan({"origin": url + "/", "genierails": UPSTREAM}, url) == []
    assert sr.plan({"origin": UPSTREAM}, "") == [("rename", "origin", "genierails")]
    assert sr.plan({"origin": UPSTREAM}, "", auto=False) == []
    assert sr.plan({"origin": UPSTREAM, "genierails": UPSTREAM}, "") == []
    assert sr.plan({"origin": url}, "") == [] and sr.plan(None, "") == []
    for remotes in ({"origin": "https://example.com/x.git"}, {"origin": UPSTREAM, "genierails": UPSTREAM}):
        with pytest.raises(sr.Refused):
            sr.plan(remotes, url)
