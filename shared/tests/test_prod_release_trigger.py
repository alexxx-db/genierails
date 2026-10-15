"""The sample prod pipelines release on envs/prod/** and shared/** changes.

Design 18.2: a product or mapping change under shared/ can change prod's
generated output, so the prod release stage runs for it too, not only for
envs/prod/**. The scope step of each sample (GitHub Actions and Azure DevOps,
aws/ and azure/) runs here, as shipped, against real commits.

It diffs from the commit of the last successful release run, not from the
commit's parent: a commit whose dev run was refused as obsolete never reaches
prod release itself, so its prod change is released through a later run's
range. That commit is a structured field GitHub or Azure DevOps records
(head_sha, sourceVersion), never a run's title, which a PR or commit title
can shape; runs from before the range check never count.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
CLOUDS = ("aws", "azure")
# The machine's own git config (hooks, signing) stays out of the sample repo.
GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
# A commit no test repo contains (a force-pushed or foreign release).
FOREIGN = "f" * 40
ADO_MACROS = {"$(System.CollectionUri)": "https://dev.azure.com/acme/",
              "$(System.TeamProjectId)": "proj-id", "$(System.DefinitionId)": "7"}
GITHUB_WORKFLOW = ".github/workflows/prod-release.yml"
# The file at fc967da, before the range check: no marker.
LEGACY_WORKFLOW = "name: GenieRails prod release\n# diffs HEAD^ HEAD\n"

needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="the fake gh applies the workflow's --jq with jq")


def _github_workflow(cloud):
    return yaml.safe_load((ROOT / cloud / ".github/workflows/prod-release.yml").read_text())


def _github_scope(cloud):
    workflow = _github_workflow(cloud)
    assert workflow["defaults"]["run"]["working-directory"] == cloud
    step = next(s for s in workflow["jobs"]["scope"]["steps"] if s.get("id") == "diff")
    assert step["env"]["DEV_SHA"] == "${{ github.event.workflow_run.head_sha }}"
    return step["run"].replace("${{ github.repository }}", "acme/governance")


def _devops_scope(cloud):
    pipeline = yaml.safe_load((ROOT / cloud / "azure-pipelines/prod-release.yml").read_text())
    stage = next(s for s in pipeline["stages"] if s["stage"] == "Scope")
    step = next(s for s in stage["jobs"][0]["steps"] if s.get("name") == "diff")
    assert step["workingDirectory"] == cloud
    assert step["env"]["SYSTEM_ACCESSTOKEN"] == "$(System.AccessToken)"
    return step["script"]


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True,
                          env=GIT_ENV).stdout.strip()


def _commit(repo, touched, files=None):
    for rel in touched:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write("change\n")
    for rel, text in (files or {}).items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", ", ".join([*touched, *(files or {})]))
    return _git(repo, "rev-parse", "HEAD")


def _pipeline_files(cloud, legacy=False):
    """The prod workflow as a customer commits it: GitHub's copy at the root, Azure DevOps' in place."""
    if legacy:
        return {GITHUB_WORKFLOW: LEGACY_WORKFLOW, f"{cloud}/azure-pipelines/prod-release.yml": LEGACY_WORKFLOW}
    return {GITHUB_WORKFLOW: (ROOT / cloud / ".github/workflows/prod-release.yml").read_text(),
            f"{cloud}/azure-pipelines/prod-release.yml": (ROOT / cloud / "azure-pipelines/prod-release.yml").read_text()}


def _repo(tmp_path, cloud, *commits, legacy=False):
    """A repo with a base commit, then one commit per list of touched paths; returns it and every SHA."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    shas = [_commit(repo, ["aws/Makefile", "azure/Makefile", "shared/generate_abac.py",
                           "aws/envs/prod/env.auto.tfvars", "azure/envs/prod/env.auto.tfvars"],
                    _pipeline_files(cloud, legacy))]
    shas += [_commit(repo, touched) for touched in commits]
    return repo, shas


def _fake_bin(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    # gh: answer the runs query from a canned API response, through the
    # workflow's own --jq filter. curl: print a canned Azure DevOps response.
    (bin_dir / "gh").write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$FAKE_ARGS"\n[ -z "$FAKE_FAIL" ] || exit 4\n'
        'while [ "$1" != --jq ]; do shift; done\nexec jq -r "$2" "$FAKE_RESPONSE"\n')
    (bin_dir / "curl").write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$FAKE_ARGS"\n[ -z "$FAKE_FAIL" ] || exit 22\n'
        'cat "$FAKE_RESPONSE"\n')
    for tool in ("gh", "curl"):
        (bin_dir / tool).chmod(0o755)
    return bin_dir


def _env(tmp_path, response, fail=False):
    (tmp_path / "response.json").write_text(json.dumps(response))
    return {**GIT_ENV, "PATH": f"{_fake_bin(tmp_path)}:{os.environ['PATH']}",
            "FAKE_RESPONSE": str(tmp_path / "response.json"), "FAKE_ARGS": str(tmp_path / "args"),
            "FAKE_FAIL": "1" if fail else "", "SYSTEM_ACCESSTOKEN": "ado-token",
            "GITHUB_WORKFLOW_REF": f"acme/governance/{GITHUB_WORKFLOW}@refs/heads/main"}


def _run(head_sha, title=None, **fields):
    """One entry of GitHub's runs list: a successful prod release run on main by default."""
    return {"head_sha": head_sha, "display_title": title or f"GenieRails prod release for {FOREIGN}",
            "conclusion": "success", "head_branch": "main", "event": "workflow_run", **fields}


def _github(tmp_path, cloud, repo, runs, run_sha=None):
    """The GitHub scope step; runs: the runs API's entries, newest first. Returns (result, prod)."""
    dev_sha = _git(repo, "rev-parse", "HEAD")
    env = _env(tmp_path, {"workflow_runs": runs}, fail=runs is None)
    output = tmp_path / "github_output"
    output.write_text("")
    result = subprocess.run(["bash", "-e", "-c", _github_scope(cloud)], cwd=repo / cloud, text=True,
                            capture_output=True, env={**env, "GITHUB_OUTPUT": str(output),
                                                      "GITHUB_SHA": run_sha or dev_sha, "DEV_SHA": dev_sha})
    return result, "prod=true" in output.read_text()


def _releases_github(tmp_path, cloud, repo, released, extra_runs=()):
    """prod=true from the GitHub scope step; released: earlier successful runs' head_shas, newest first."""
    runs = [*extra_runs, *(_run(sha) for sha in released)]
    result, prod = _github(tmp_path, cloud, repo, runs)
    assert result.returncode == 0, result.stdout + result.stderr
    args = (tmp_path / "args").read_text()
    assert ("repos/acme/governance/actions/workflows/prod-release.yml/runs"
            "?branch=main&event=workflow_run&status=success&per_page=100") in args
    return prod


def _devops(tmp_path, cloud, repo, builds):
    sha = _git(repo, "rev-parse", "HEAD")
    script = (_devops_scope(cloud).replace("$(Build.SourceVersion)", sha)
              .replace("$(resources.pipeline.dev.sourceCommit)", sha))
    for macro, value in ADO_MACROS.items():
        script = script.replace(macro, value)
    env = _env(tmp_path, {"count": len(builds or ()), "value": builds or []}, fail=builds is None)
    # Azure DevOps runs a script step with plain bash: no -e.
    result = subprocess.run(["bash", "-c", script], cwd=repo / cloud, text=True, capture_output=True, env=env)
    return result, "##vso[task.setvariable variable=prod;isOutput=true]true" in result.stdout


def _build(sha, **fields):
    return {"sourceVersion": sha, "result": "succeeded", "sourceBranch": "refs/heads/main",
            "buildNumber": f"GenieRails prod release at {FOREIGN}", **fields}


def _releases_devops(tmp_path, cloud, repo, released, extra_builds=()):
    result, prod = _devops(tmp_path, cloud, repo, [*extra_builds, *(_build(s) for s in released)])
    assert result.returncode == 0, result.stdout + result.stderr
    args = (tmp_path / "args").read_text()
    assert ("https://dev.azure.com/acme/proj-id/_apis/build/builds?definitions=7&branchName=refs/heads/main"
            "&resultFilter=succeeded&statusFilter=completed&queryOrder=finishTimeDescending&$top=100"
            "&api-version=7.1") in args
    assert "Authorization: Bearer ado-token" in args
    return prod


@needs_jq
@pytest.mark.parametrize("cloud", CLOUDS)
@pytest.mark.parametrize("touched, releases", [
    ("shared/generate_abac.py", True),
    ("shared/treatment_config.json", True),
    ("{cloud}/envs/prod/env.auto.tfvars", True),
    ("{cloud}/envs/dev/env.auto.tfvars", False),
    ("{other}/envs/prod/env.auto.tfvars", False),
])
def test_prod_release_scope_covers_shared_and_envs_prod(tmp_path, cloud, touched, releases):
    other = "azure" if cloud == "aws" else "aws"
    repo, (base, _) = _repo(tmp_path, cloud, [touched.format(cloud=cloud, other=other)])
    assert _releases_github(tmp_path, cloud, repo, [base]) is releases
    assert _releases_devops(tmp_path, cloud, repo, [base]) is releases


@needs_jq
@pytest.mark.parametrize("cloud", CLOUDS)
@pytest.mark.parametrize("released, releases", [
    # The last release was before the prod change: a dev-only head still
    # releases it (a parent-only diff sees just the dev change).
    (["base"], True),
    # Already released through the prod change: nothing new for prod.
    (["prod"], False),
    # Newest first; the newest ancestor is the base.
    (["prod", "base"], False),
    # A re-run of the head's own release never counts as its base.
    (["head", "base"], True),
    # No earlier release, or none that is an ancestor: a prod change.
    ([], True),
    (["foreign"], True),
    (["foreign", "prod"], False),
])
def test_prod_release_scope_diffs_from_the_last_successful_release(tmp_path, cloud, released, releases):
    repo, (base, prod, head) = _repo(tmp_path, cloud, [f"{cloud}/envs/prod/env.auto.tfvars"],
                                     [f"{cloud}/envs/dev/env.auto.tfvars"])
    named = {"base": base, "prod": prod, "head": head, "foreign": FOREIGN}
    shas = [named[r] for r in released]
    assert _releases_github(tmp_path, cloud, repo, shas) is releases
    assert _releases_devops(tmp_path, cloud, repo, shas) is releases


@needs_jq
@pytest.mark.parametrize("cloud", CLOUDS)
@pytest.mark.parametrize("released, title_names, releases", [
    # The real last release is before the prod change. Titles naming the
    # prod commit (a PR title, a pre-fix run-name, the old canonical form)
    # must not narrow the range past it.
    ("base", "prod", True),
    # Already released through the prod change. Titles naming an older
    # commit must not widen it back.
    ("prod", "base", False),
])
@pytest.mark.parametrize("title", [
    "PR-controlled at {sha}",
    "GenieRails prod release ({cloud}) at {sha}",
    "{sha}",
])
def test_prod_release_base_ignores_run_titles(tmp_path, cloud, released, title_names, releases, title):
    repo, (base, prod, _) = _repo(tmp_path, cloud, [f"{cloud}/envs/prod/env.auto.tfvars"],
                                  [f"{cloud}/envs/dev/env.auto.tfvars"])
    named = {"base": base, "prod": prod}
    crafted = title.format(cloud=cloud, sha=named[title_names])
    # Newest first: the crafted titles sit on newer successful runs, whose
    # head_sha is the real release or not a commit of this repo.
    runs = [_run(FOREIGN, crafted), _run(named[released], crafted)]
    assert _github(tmp_path, cloud, repo, runs)[1] is releases
    builds = [_build(FOREIGN, buildNumber=crafted), _build(named[released], buildNumber=crafted)]
    assert _releases_devops(tmp_path, cloud, repo, [], extra_builds=builds) is releases


@needs_jq
@pytest.mark.parametrize("cloud", CLOUDS)
@pytest.mark.parametrize("fields", [
    {"conclusion": "failure"}, {"conclusion": "cancelled"}, {"conclusion": "skipped"},
    {"head_branch": "attacker/branch"}, {"event": "workflow_dispatch"}, {"event": "pull_request"},
])
def test_prod_release_base_counts_only_successful_workflow_run_runs_on_main(tmp_path, cloud, fields):
    repo, (base, prod, _) = _repo(tmp_path, cloud, [f"{cloud}/envs/prod/env.auto.tfvars"],
                                  [f"{cloud}/envs/dev/env.auto.tfvars"])
    # Even if the API ignored the query's filters, such a run at the prod
    # commit can't narrow the range: the real base is before it.
    assert _releases_github(tmp_path, cloud, repo, [base], extra_runs=[_run(prod, **fields)]) is True
    ado = {"conclusion": {"result": "failed"}, "head_branch": {"sourceBranch": "refs/heads/attacker"},
           "event": {"sourceBranch": "refs/pull/1/merge"}}[next(iter(fields))]
    assert _releases_devops(tmp_path, cloud, repo, [base], extra_builds=[_build(prod, **ado)]) is True


@needs_jq
@pytest.mark.parametrize("cloud", CLOUDS)
def test_runs_from_before_the_range_check_never_count(tmp_path, cloud):
    # A pre-fix run checked only HEAD^..HEAD and ran at main's head, so its
    # head_sha says nothing about what it released.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, ["aws/Makefile", "azure/Makefile", "shared/generate_abac.py"], _pipeline_files(cloud, legacy=True))
    legacy = _commit(repo, [f"{cloud}/envs/prod/env.auto.tfvars"])
    upgraded = _commit(repo, [], _pipeline_files(cloud))
    _commit(repo, [f"{cloud}/envs/dev/env.auto.tfvars"])
    # Only legacy runs: no base, so a prod change.
    assert _releases_github(tmp_path, cloud, repo, [legacy]) is True
    assert _releases_devops(tmp_path, cloud, repo, [legacy]) is True
    # A run at a commit with the range check counts.
    assert _releases_github(tmp_path, cloud, repo, [upgraded, legacy]) is False
    assert _releases_devops(tmp_path, cloud, repo, [upgraded, legacy]) is False


@needs_jq
@pytest.mark.parametrize("cloud", CLOUDS)
@pytest.mark.parametrize("later, ok", [
    # GitHub ran this release at a newer main commit (the run's head_sha).
    # It may succeed only if nothing for prod changed after the dev commit.
    ("README.md", True),
    ("{cloud}/envs/dev/env.auto.tfvars", True),
    ("{cloud}/envs/prod/env.auto.tfvars", False),
    ("shared/generate_abac.py", False),
])
def test_a_run_at_a_newer_main_commit_succeeds_only_without_later_prod_changes(tmp_path, cloud, later, ok):
    repo, (base, dev) = _repo(tmp_path, cloud, [f"{cloud}/envs/dev/env.auto.tfvars"])
    newer = _commit(repo, [later.format(cloud=cloud)])
    _git(repo, "checkout", "-q", "--detach", dev)
    result, prod = _github(tmp_path, cloud, repo, [_run(base)], run_sha=newer)
    assert (result.returncode == 0) is ok, result.stdout + result.stderr
    assert prod is False
    if not ok:
        assert f"main moved on to {newer}" in result.stdout


@needs_jq
@pytest.mark.parametrize("cloud", CLOUDS)
def test_a_run_whose_commit_does_not_contain_the_dev_commit_fails(tmp_path, cloud):
    repo, (base, dev) = _repo(tmp_path, cloud, [f"{cloud}/envs/dev/env.auto.tfvars"])
    _git(repo, "checkout", "-q", "-b", "side", base)
    side = _commit(repo, ["README.md"])
    _git(repo, "checkout", "-q", "--detach", dev)
    result, prod = _github(tmp_path, cloud, repo, [_run(base)], run_sha=side)
    assert result.returncode != 0 and not prod
    assert "does not contain the dev commit" in result.stdout


@needs_jq
@pytest.mark.parametrize("cloud", CLOUDS)
def test_prod_release_scope_fails_when_it_cannot_list_earlier_runs(tmp_path, cloud):
    repo, _ = _repo(tmp_path, cloud, ["aws/envs/dev/env.auto.tfvars"])
    for result, prod in (_github(tmp_path, cloud, repo, None), _devops(tmp_path, cloud, repo, None)):
        assert result.returncode != 0
        assert not prod


@pytest.mark.parametrize("cloud", CLOUDS)
def test_a_failed_dev_run_fails_the_release_run(cloud):
    # A run that succeeded with every job skipped would count as a base.
    jobs = _github_workflow(cloud)["jobs"]
    assert jobs["scope"]["if"] == "github.event.workflow_run.conclusion == 'success'"
    failed = jobs["dev-failed"]
    assert failed["if"] == "github.event.workflow_run.conclusion != 'success'"
    assert failed["steps"][-1]["run"].rstrip().endswith("exit 1")


@pytest.mark.parametrize("cloud", CLOUDS)
def test_the_scope_never_reads_run_titles(cloud):
    scope = _github_scope(cloud)
    assert "display_title" not in scope and "name" not in scope.replace("--name-only", "")
    assert "buildNumber" not in _devops_scope(cloud)


@pytest.mark.parametrize("cloud", CLOUDS)
def test_prod_release_scope_has_full_history_and_reads_runs(cloud):
    scope = _github_workflow(cloud)["jobs"]["scope"]
    checkout = next(s for s in scope["steps"] if s.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 0
    assert scope["permissions"] == {"contents": "read", "actions": "read"}
    pipeline = yaml.safe_load((ROOT / cloud / "azure-pipelines/prod-release.yml").read_text())
    stage = next(s for s in pipeline["stages"] if s["stage"] == "Scope")
    assert stage["jobs"][0]["steps"][0] == {"checkout": "self", "fetchDepth": 0}


@pytest.mark.parametrize("cloud", CLOUDS)
def test_prod_release_headers_name_both_triggers(cloud):
    for path in (ROOT / cloud / ".github/workflows/prod-release.yml",
                 ROOT / cloud / "azure-pipelines/prod-release.yml"):
        text = " ".join(path.read_text().replace("#", " ").split())
        assert "envs/prod/** or shared/**" in text, path
        assert "Did envs/prod or shared change since the last" in text, path
