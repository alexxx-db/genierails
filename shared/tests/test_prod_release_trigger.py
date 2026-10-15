"""The sample prod pipelines release on envs/prod/** and shared/** changes.

Design 18.2: a product or mapping change under shared/ can change prod's
generated output, so the prod release stage runs for it too, not only for
envs/prod/**. The scope step of each sample (GitHub Actions and Azure DevOps,
aws/ and azure/) runs here, as shipped, against real commits.

It diffs from the commit of the last successful release run, not from the
commit's parent: a commit whose dev run was refused as obsolete never reaches
prod release itself, so its prod change is released through a later run's
range.
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

needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="the fake gh applies the workflow's --jq with jq")


def _github_workflow(cloud):
    return yaml.safe_load((ROOT / cloud / ".github/workflows/prod-release.yml").read_text())


def _github_scope(cloud):
    workflow = _github_workflow(cloud)
    assert workflow["defaults"]["run"]["working-directory"] == cloud
    step = next(s for s in workflow["jobs"]["scope"]["steps"] if s.get("id") == "diff")
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


def _commit(repo, touched):
    for rel in touched:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write("change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", ", ".join(touched))
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path, *commits):
    """A repo with a base commit, then one commit per list of touched paths; returns it and every SHA."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    shas = [_commit(repo, ["aws/Makefile", "azure/Makefile", "shared/generate_abac.py",
                           "aws/envs/prod/env.auto.tfvars", "azure/envs/prod/env.auto.tfvars"])]
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
            "GITHUB_WORKFLOW_REF": "acme/governance/.github/workflows/prod-release.yml@refs/heads/main"}


def _run_name(cloud, sha):
    """A successful run's name, as the workflow's run-name renders it."""
    run_name = _github_workflow(cloud)["run-name"]
    assert "format('{0} at {1}', github.workflow, github.event.workflow_run.head_sha)" in run_name
    return f"GenieRails prod release ({cloud}) at {sha}"


def _releases_github(tmp_path, cloud, repo, released, fail=False):
    """prod=true from the GitHub scope step; released: earlier successful runs' commits, newest first."""
    titles = [_run_name(cloud, sha) for sha in released]
    # A run whose dev run failed names no commit and never counts.
    titles.insert(0, f"GenieRails prod release ({cloud}) (dev run did not succeed)")
    env = _env(tmp_path, {"workflow_runs": [{"display_title": t} for t in titles]}, fail)
    output = tmp_path / "github_output"
    output.write_text("")
    result = subprocess.run(["bash", "-e", "-c", _github_scope(cloud)], cwd=repo / cloud, text=True,
                            capture_output=True, env={**env, "GITHUB_OUTPUT": str(output)})
    if fail:
        return result, output.read_text()
    assert result.returncode == 0, result.stderr
    args = (tmp_path / "args").read_text()
    assert "repos/acme/governance/actions/workflows/prod-release.yml/runs?status=success" in args
    return "prod=true" in output.read_text()


def _releases_devops(tmp_path, cloud, repo, released, fail=False):
    sha = _git(repo, "rev-parse", "HEAD")
    script = (_devops_scope(cloud).replace("$(Build.SourceVersion)", sha)
              .replace("$(resources.pipeline.dev.sourceCommit)", sha))
    for macro, value in ADO_MACROS.items():
        script = script.replace(macro, value)
    env = _env(tmp_path, {"count": len(released), "value": [{"sourceVersion": s} for s in released]}, fail)
    # Azure DevOps runs a script step with plain bash: no -e.
    result = subprocess.run(["bash", "-c", script], cwd=repo / cloud, text=True, capture_output=True, env=env)
    if fail:
        return result, result.stdout
    assert result.returncode == 0, result.stdout + result.stderr
    args = (tmp_path / "args").read_text()
    assert ("https://dev.azure.com/acme/proj-id/_apis/build/builds?definitions=7&resultFilter=succeeded"
            "&statusFilter=completed&queryOrder=finishTimeDescending&$top=100&api-version=7.1") in args
    assert "Authorization: Bearer ado-token" in args
    return "##vso[task.setvariable variable=prod;isOutput=true]true" in result.stdout


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
    repo, (base, _) = _repo(tmp_path, [touched.format(cloud=cloud, other=other)])
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
    repo, (base, prod, head) = _repo(tmp_path, [f"{cloud}/envs/prod/env.auto.tfvars"],
                                     [f"{cloud}/envs/dev/env.auto.tfvars"])
    named = {"base": base, "prod": prod, "head": head, "foreign": FOREIGN}
    shas = [named[r] for r in released]
    assert _releases_github(tmp_path, cloud, repo, shas) is releases
    assert _releases_devops(tmp_path, cloud, repo, shas) is releases


@needs_jq
@pytest.mark.parametrize("cloud", CLOUDS)
def test_prod_release_scope_fails_when_it_cannot_list_earlier_runs(tmp_path, cloud):
    repo, (base, _) = _repo(tmp_path, ["aws/envs/dev/env.auto.tfvars"])
    for result, output in (_releases_github(tmp_path, cloud, repo, [base], fail=True),
                           _releases_devops(tmp_path, cloud, repo, [base], fail=True)):
        assert result.returncode != 0
        assert "prod=true" not in output


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
