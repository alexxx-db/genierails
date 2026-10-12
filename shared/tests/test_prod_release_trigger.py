"""The sample prod pipelines release on envs/prod/** and shared/** changes.

Design 18.2: a product or mapping change under shared/ can change prod's
generated output, so the prod release stage runs for it too, not only for
envs/prod/**. The scope step of each sample (GitHub Actions and Azure DevOps,
aws/ and azure/) runs here, as shipped, against real commits.
"""

import os
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


def _github_scope(cloud):
    workflow = yaml.safe_load((ROOT / cloud / ".github/workflows/prod-release.yml").read_text())
    assert workflow["defaults"]["run"]["working-directory"] == cloud
    step = next(s for s in workflow["jobs"]["scope"]["steps"] if s.get("id") == "diff")
    return step["run"]


def _devops_scope(cloud):
    pipeline = yaml.safe_load((ROOT / cloud / "azure-pipelines/prod-release.yml").read_text())
    stage = next(s for s in pipeline["stages"] if s["stage"] == "Scope")
    step = next(s for s in stage["jobs"][0]["steps"] if s.get("name") == "diff")
    assert step["workingDirectory"] == cloud
    return step["script"]


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=GIT_ENV)


def _repo_with_commit(tmp_path, touched):
    repo = tmp_path / "repo"
    for rel in ("aws/Makefile", "azure/Makefile", "shared/generate_abac.py",
                "aws/envs/prod/env.auto.tfvars", "azure/envs/prod/env.auto.tfvars"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("base\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    path = repo / touched
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                         check=True, env=GIT_ENV).stdout.strip()
    return repo, sha


def _releases_github(tmp_path, cloud, repo):
    output = tmp_path / "github_output"
    output.write_text("")
    result = subprocess.run(["bash", "-e", "-c", _github_scope(cloud)], cwd=repo / cloud, text=True,
                            capture_output=True, env={**GIT_ENV, "GITHUB_OUTPUT": str(output)})
    assert result.returncode == 0, result.stderr
    return "prod=true" in output.read_text()


def _releases_devops(cloud, repo, sha):
    script = (_devops_scope(cloud).replace("$(Build.SourceVersion)", sha)
              .replace("$(resources.pipeline.dev.sourceCommit)", sha))
    result = subprocess.run(["bash", "-e", "-c", script], cwd=repo / cloud, text=True, capture_output=True,
                            env=GIT_ENV)
    assert result.returncode == 0, result.stdout + result.stderr
    return "##vso[task.setvariable variable=prod;isOutput=true]true" in result.stdout


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
    repo, sha = _repo_with_commit(tmp_path, touched.format(cloud=cloud, other=other))
    assert _releases_github(tmp_path, cloud, repo) is releases
    assert _releases_devops(cloud, repo, sha) is releases


@pytest.mark.parametrize("cloud", CLOUDS)
def test_prod_release_headers_name_both_triggers(cloud):
    for path in (ROOT / cloud / ".github/workflows/prod-release.yml",
                 ROOT / cloud / "azure-pipelines/prod-release.yml"):
        text = path.read_text()
        assert "envs/prod/**" in text and "shared/**" in text, path
        assert "Did the commit touch envs/prod or shared?" in text, path
