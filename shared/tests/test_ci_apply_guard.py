import os
import re
import signal
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
APPLYING_TARGETS = (
    "enable-classification", "rehearse", "release", "release-plan", "_release-manifest", "maintain", "apply",
    "apply-governance", "apply-genie", "_apply-layer", "integration-test",
    "test-champion", "test-all", "test-ci", "test-ci-parallel",
    "destroy", "destroy-governance", "destroy-genie", "_destroy-layer",
    "import", "migrate-state", "migrate-state-dir", "ungovern", "apply-account", "policy-abandon",
)
CI_MARKERS = ("CI", "TF_BUILD", "JENKINS_URL", "GITLAB_CI", "BUILDKITE", "CIRCLECI")


def _clean_env(**updates):
    env = {key: value for key, value in os.environ.items() if key not in (*CI_MARKERS, "GENIERAILS_ALLOW_CI_APPLY")}
    env.update(updates)
    return env


def _run_guarded_make(cloud, target, *, env, timeout=10):
    proc = subprocess.Popen(
        ["make", "-f", str(ROOT / cloud / "Makefile"), target, "ENV=dev"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        pytest.fail(f"timed out and killed process group for {cloud} make {target}")
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


@pytest.mark.parametrize("cloud", ["aws", "azure"])
@pytest.mark.parametrize("target", APPLYING_TARGETS)
def test_every_applying_target_refuses_in_ci(cloud, target):
    proc = _run_guarded_make(cloud, target, env=_clean_env(CI="true"))
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "applying targets cannot run in CI" in output


@pytest.mark.parametrize("cloud", ["aws", "azure"])
@pytest.mark.parametrize("target", [
    ["plan"], ["validate"], ["test-unit"], ["coverage-gate"], ["audit-schema"], ["check"],
    ["promote-to", "CHECK=1"],
])
def test_read_only_targets_have_no_ci_refusal(cloud, target):
    proc = subprocess.run(
        ["make", "-n", "-f", str(ROOT / cloud / "Makefile"), *target, "ENV=dev", "CI=true"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert "applying targets cannot run in CI" not in proc.stdout + proc.stderr


@pytest.mark.parametrize("cloud", ["aws", "azure"])
def test_throwaway_integration_workflow_has_explicit_ci_apply_opt_out(cloud):
    proc = _run_guarded_make(
        cloud,
        "_guard-not-ci-apply",
        env=_clean_env(CI="true", GENIERAILS_ALLOW_CI_APPLY="1"),
    )
    assert proc.returncode == 0


@pytest.mark.parametrize(("marker", "value"), [
    ("CI", "true"), ("CI", "1"), ("CI", "yes"), ("CI", "TrUe"),
    ("TF_BUILD", "True"), ("JENKINS_URL", "https://jenkins.example"),
    ("GITLAB_CI", "true"), ("BUILDKITE", "true"), ("CIRCLECI", "true"),
])
def test_guard_detects_common_ci_markers(marker, value):
    proc = _run_guarded_make("aws", "_guard-not-ci-apply", env=_clean_env(**{marker: value}))
    assert proc.returncode != 0
    assert "applying targets cannot run in CI" in proc.stderr


def test_ci_workflow_scopes_apply_opt_out_to_integration_steps():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert workflow.count('GENIERAILS_ALLOW_CI_APPLY: "1"') == 2
    unit_job = workflow[workflow.index("  unit-tests:"):workflow.index("  validation:")]
    assert "GENIERAILS_ALLOW_CI_APPLY" not in unit_job


SAMPLE_DIRS = (".github", "azure-pipelines")
# GitHub Actions steps sit at six spaces; Azure DevOps steps start with a step keyword.
STEP_SPLIT = {
    ".github": r"(?m)^      - ",
    "azure-pipelines": r"(?m)^\s*- (?:script|bash|pwsh|powershell|task|checkout|template|download|publish):",
}


def _sample_workflows():
    return sorted(
        (sample_dir, path)
        for cloud in ("aws", "azure")
        for sample_dir in SAMPLE_DIRS
        for suffix in ("*.yml", "*.yaml")
        for path in (ROOT / cloud / sample_dir).rglob(suffix)
    )


def test_shipped_workflows_scope_the_only_applying_command_to_dev_rehearse():
    workflow_paths = _sample_workflows()
    assert {sample_dir for sample_dir, _ in workflow_paths} == set(SAMPLE_DIRS)
    for sample_dir, path in workflow_paths:
        text = path.read_text()
        steps = re.split(STEP_SPLIT[sample_dir], text)[1:]
        assert steps, f"{path}: no steps found"
        executed_targets = set()
        for step in steps:
            targets = re.findall(r"\bmake\s+(?:--no-print-directory\s+)?([A-Za-z_][\w-]*)", step)
            executed_targets.update(targets)
            opted_out = bool(re.search(r"GENIERAILS_ALLOW_CI_APPLY:\s*['\"]?1['\"]?", step))
            guarded = set(targets) & set(APPLYING_TARGETS)
            assert not guarded or guarded == {"rehearse"} and "dev-rehearse" in path.name, path
        # Customer samples never use the integration-test bypass. Only stage 3
        # may apply, and the make guard independently proves dev locked state.
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert "GENIERAILS_ALLOW_CI_APPLY" not in code, path
        assert (executed_targets & set(APPLYING_TARGETS)) <= ({"rehearse"} if "dev-rehearse" in path.name else set())


@pytest.mark.parametrize("stage", ["prod-release", "prod-maintain"])
def test_applying_stage_samples_are_marked_as_needing_remote_state(stage):
    for cloud in ("aws", "azure"):
        for sample_dir in SAMPLE_DIRS:
            text = (ROOT / cloud / sample_dir / ("workflows" if sample_dir == ".github" else "") / f"{stage}.yml").read_text()
            assert "REQUIRES DEV REMOTE STATE" in text or "REQUIRES PROD REMOTE STATE" in text
            assert "deployment machine" in text
            assert "make check" in text or "make audit-schema" in text


def test_dev_samples_queue_every_commit_and_never_cancel():
    for cloud in ("aws", "azure"):
        github = (ROOT / cloud / ".github" / "workflows" / "dev-rehearse.yml").read_text()
        assert "concurrency:" not in github and "cancel-in-progress: true" not in github
        assert "needs: queue" in github
        devops = (ROOT / cloud / "azure-pipelines" / "dev-rehearse.yml").read_text()
        assert "lockBehavior: sequential" in devops and "batch: false" in devops
        assert "envs/account/remote_state.hcl" in github + devops
        assert github.count("FETCH_HEAD") and devops.count("FETCH_HEAD")


def test_prod_samples_do_not_consume_unsigned_receipt_artifacts():
    for cloud in ("aws", "azure"):
        github = (ROOT / cloud / ".github/workflows/prod-release.yml").read_text()
        ado = (ROOT / cloud / "azure-pipelines/prod-release.yml").read_text()
        assert "download-artifact" not in github
        assert "download: dev" not in ado
        assert re.search(r"checkout: self\n\s+fetchDepth: 0", ado)
        assert 'changed="$(git diff --name-only "$base" HEAD -- envs/prod ../shared)" || exit $?' in ado


def _concurrency_group(text):
    block = re.search(r"(?m)^concurrency:\n((?:[ \t]+.*\n)+)", text)
    return block and re.search(r"(?m)^\s+group:\s*(\S+)", block.group(1)).group(1)


def test_prod_release_and_maintain_do_not_share_a_concurrency_group():
    # GitHub keeps one pending run per group and cancels the older one, so a
    # shared group (or any group on release) can cancel a pending release.
    for cloud in ("aws", "azure"):
        github = ROOT / cloud / ".github" / "workflows"
        release = (github / "prod-release.yml").read_text()
        maintain = (github / "prod-maintain.yml").read_text()
        assert _concurrency_group(release) is None, cloud
        assert _concurrency_group(maintain) == f"genierails-prod-maintain-{cloud}"
        assert "cancel-in-progress: true" not in release + maintain
        devops = ROOT / cloud / "azure-pipelines"
        release = (devops / "prod-release.yml").read_text()
        assert "lockBehavior: sequential" in release and "lockBehavior: runLatest" not in release
        assert "environment:" not in (devops / "prod-maintain.yml").read_text()


def test_samples_use_oidc_not_secrets():
    for _, path in _sample_workflows():
        text = path.read_text()
        assert "${{ secrets." not in text and "$(secrets" not in text and "CLIENT_SECRET:" not in text, path
        if "make check" in text or "make audit-schema" in text:
            assert "github-oidc" in text or "azure-devops-oidc" in text, path


def test_workspace_guard_runs_env_validator_and_rejects_bad_tfvars(tmp_path):
    cloud_root = tmp_path / "aws"
    env_dir = cloud_root / "envs" / "dev"
    env_dir.mkdir(parents=True)
    (env_dir / "env.auto.tfvars").write_text('governance_mode = "future"\n')
    proc = subprocess.run(
        [
            "make", "-f", str(ROOT / "aws" / "Makefile"),
            "_guard-workspace-config", "ENV=dev",
            f"CLOUD_ROOT={cloud_root}", f"ENV_DIR={env_dir}",
            f"SHARED_ROOT={ROOT / 'shared'}",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert proc.returncode != 0
    assert "env config validation: governance_mode must be legacy or deterministic" in proc.stderr


def test_env_validator_cli_uses_nonzero_exit_and_stderr(tmp_path):
    env_file = tmp_path / "env.auto.tfvars"
    env_file.write_text('treatment_versions = { ssn = { partial = ["bad"] } }\n')
    proc = subprocess.run(
        [sys.executable, str(ROOT / "shared" / "scripts" / "validate_env_config.py"), str(env_file)],
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "treatment_versions" in proc.stderr
    assert "Traceback" not in proc.stderr
