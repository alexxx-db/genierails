"""State-changing targets refuse on Databricks compute; read-only ones still run.

A notebook, Job, serverless run or App keeps no local disk past the run, so
the receipts, manifests, staged journal, env locks and Genie ID records a
state-changing target writes would be lost after its live change
(scripts/databricks_compute.py). Each refusal is checked on a real throwaway
repo (the real aws/Makefile and shared/): nothing under it changes, and
neither terraform, the databricks CLI nor the Databricks SDK is reached.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from shared.tests.test_check_target import DEV_ENV, _build

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "shared" / "scripts" / "terraform_layer.sh"
MARKERS = {"DATABRICKS_RUNTIME_VERSION": "15.4", "DB_CLUSTER_ID": "0101-123456-abcdefgh",
           "DB_IS_JOB_CLUSTER": "TRUE", "DATABRICKS_APP_NAME": "genierails"}
REFUSED = "refused: this is Databricks compute"
OPT_IN = "GENIERAILS_ALLOW_DATABRICKS_COMPUTE"
# Everything _guard-not-ci-apply guards, plus the live writers CI doesn't run
# (sync-tags, bootstrap-sp, bootstrap-pipelines) and account-plan's locked manifest.
STATE_CHANGING = (
    "enable-classification", "rehearse", "release", "release-plan", "_release-manifest", "maintain", "apply",
    "apply-governance", "apply-genie", "_apply-layer", "integration-test",
    "test-champion", "test-all", "test-ci", "test-ci-parallel",
    "destroy", "destroy-governance", "destroy-genie", "_destroy-layer",
    "import", "migrate-state", "migrate-state-dir", "ungovern", "apply-account", "policy-abandon", "policy-rebind",
    "detach-agent", "split-agent-files",
    "sync-tags", "_sync-tags-add-only-locked", "bootstrap-sp", "bootstrap-pipelines", "account-plan",
    "_account-plan-locked",
)
CI_VARS = ("CI", "TF_BUILD", "JENKINS_URL", "GITLAB_CI", "BUILDKITE", "CIRCLECI", "GITHUB_ACTIONS",
           "GENIERAILS_ALLOW_CI_APPLY")
# Logs any call; a refusal must leave the log empty.
FAKE_CLI = '#!/bin/sh\necho "$(basename "$0") $*" >> "$CALL_LOG"\nexit 0\n'


def _env(home, **extra):
    drop = ("MAKEFLAGS", "MAKELEVEL", "MFLAGS", "ENV", "GENIERAILS_STATE_DIR", "GENIERAILS_DURABLE_STATE_DIR",
            OPT_IN, *CI_VARS, *MARKERS)
    env = {k: v for k, v in os.environ.items() if k not in drop and not k.startswith("DATABRICKS_")}
    env.update(HOME=str(home), PYTHONDONTWRITEBYTECODE="1", **extra)
    return env


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    """A committed dev env, plus a fake terraform/databricks CLI and SDK that log any use."""
    tmp = tmp_path_factory.mktemp("dbx")
    repo = _build(tmp, DEV_ENV)
    log = tmp / "calls.log"
    (tmp / "guard-bin").mkdir()
    for tool in ("terraform", "databricks"):
        (tmp / "guard-bin" / tool).write_text(FAKE_CLI)
        (tmp / "guard-bin" / tool).chmod(0o755)
    sdk = tmp / "sdk" / "databricks"
    sdk.mkdir(parents=True)
    (sdk / "__init__.py").write_text(f"open({str(log)!r}, 'a').write('import databricks\\n')\n")
    return {"tmp": tmp, "repo": repo, "log": log}


def _snapshot(root):
    """Every path under ``root`` (not following the shared/ symlink) with its bytes."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            out[str(path.relative_to(root))] = (
                os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None)
    return out


def _make(site, *args, guarded=True, **env):
    # Refusal runs get the logging fakes; read-only runs get the fixture's terraform stub.
    bin_dir = site["tmp"] / ("guard-bin" if guarded else "bin")
    extra = dict(PATH=f"{bin_dir}:{os.environ['PATH']}", CALL_LOG=str(site["log"]),
                 FAKE_TF_LOG=str(site["tmp"] / "tf.log"))
    if guarded:
        extra["PYTHONPATH"] = str(site["tmp"] / "sdk")
    return subprocess.run(["make", "--no-print-directory", "-C", str(site["repo"] / "aws"), *args],
                          text=True, capture_output=True, env=_env(site["tmp"], **extra, **env), timeout=120)


def _shared_status():
    return subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--", "shared", "aws", "azure"],
                          text=True, capture_output=True, check=True).stdout


def _assert_refused_unchanged(site, *args, **env):
    site["log"].unlink(missing_ok=True)
    before, status = _snapshot(site["tmp"]), _shared_status()
    proc = _make(site, *args, **env)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert REFUSED in output, output
    assert "deployment machine" in output
    assert not site["log"].exists(), site["log"].read_text()
    assert _snapshot(site["tmp"]) == before
    assert _shared_status() == status
    return output


# --- every marker refuses every state-changing target, writing nothing --------

@pytest.mark.parametrize("marker", MARKERS)
@pytest.mark.parametrize("target", STATE_CHANGING)
def test_every_state_changing_target_refuses_on_databricks_compute(site, target, marker):
    output = _assert_refused_unchanged(site, target, "ENV=dev", **{marker: MARKERS[marker]})
    assert f"({marker} is set)" in output


@pytest.mark.parametrize("target", STATE_CHANGING)
def test_azure_targets_refuse_too(target):
    status = _shared_status()
    proc = subprocess.run(["make", "--no-print-directory", "-f", str(ROOT / "azure" / "Makefile"), target, "ENV=dev"],
                          cwd=ROOT, text=True, capture_output=True, timeout=60,
                          env=_env(os.environ.get("HOME", ""), DATABRICKS_RUNTIME_VERSION="client.1.13"))
    assert proc.returncode != 0
    assert REFUSED in proc.stdout + proc.stderr
    assert _shared_status() == status


def test_the_databricks_refusal_wins_over_ci_and_its_opt_out(site):
    # GENIERAILS_ALLOW_CI_APPLY=1 is for throwaway CI runners; it never lifts this.
    output = _assert_refused_unchanged(site, "apply", "ENV=dev", CI="true", GENIERAILS_ALLOW_CI_APPLY="1",
                                       DATABRICKS_RUNTIME_VERSION="15.4")
    assert "cannot run in CI" not in output


def test_dev_rehearse_ci_exception_does_not_apply(site):
    # CI may run make rehearse ENV=dev with remote state; Databricks compute may not.
    _assert_refused_unchanged(site, "rehearse", "ENV=dev", CI="true", DATABRICKS_RUNTIME_VERSION="15.4")


def test_command_line_marker_counts(site):
    _assert_refused_unchanged(site, "apply", "ENV=dev", "DATABRICKS_RUNTIME_VERSION=15.4")


def test_empty_marker_is_not_databricks(site):
    proc = _make(site, "_guard-not-ci-apply", "ENV=dev", DATABRICKS_RUNTIME_VERSION="")
    assert proc.returncode == 0, proc.stderr


# --- read-only targets still run ----------------------------------------------

@pytest.mark.parametrize("marker", MARKERS)
def test_make_check_still_runs_on_databricks_compute(site, marker):
    before = _snapshot(site["repo"] / "aws" / "envs")
    proc = _make(site, "check", "ENV=dev", guarded=False, **{marker: MARKERS[marker]})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert REFUSED not in proc.stdout + proc.stderr
    assert _snapshot(site["repo"] / "aws" / "envs") == before


@pytest.mark.parametrize("target", ["validate", "validate-generated", "coverage-gate"])
def test_offline_read_only_targets_still_run(site, target):
    proc = _make(site, target, "ENV=dev", guarded=False, DATABRICKS_RUNTIME_VERSION="15.4")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert REFUSED not in proc.stdout + proc.stderr


@pytest.mark.parametrize("cloud", ["aws", "azure"])
@pytest.mark.parametrize("target", [
    ["plan"], ["validate"], ["check"], ["evidence"], ["audit-schema"], ["coverage-gate"], ["policy-status"],
    ["promote-to", "CHECK=1"], ["verify-access-spec"],
])
def test_read_only_targets_have_no_databricks_refusal(cloud, target):
    proc = subprocess.run(["make", "-n", "-f", str(ROOT / cloud / "Makefile"), *target, "ENV=dev"],
                          cwd=ROOT, text=True, capture_output=True, timeout=30,
                          env=_env(os.environ.get("HOME", ""), DATABRICKS_RUNTIME_VERSION="15.4"))
    assert REFUSED not in proc.stdout + proc.stderr
    assert "databricks_compute.py" not in proc.stdout


# --- no markers: nothing changes on laptops, servers and CI --------------------

def test_no_marker_never_runs_the_check(site, tmp_path):
    # A python3 shim logs every script it runs: without a marker the helper isn't one of them.
    shim, log = tmp_path / "shim", tmp_path / "python.log"
    shim.mkdir()
    (shim / "python3").write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexec "{sys.executable}" "$@"\n')
    (shim / "python3").chmod(0o755)
    env = _env(site["tmp"], PATH=f"{shim}:{os.environ['PATH']}")
    for marker in ({}, {"DATABRICKS_RUNTIME_VERSION": "15.4"}):
        proc = subprocess.run(["make", "--no-print-directory", "-C", str(site["repo"] / "aws"),
                               "_guard-not-ci-apply", "ENV=dev"], text=True, capture_output=True,
                              env={**env, **marker}, timeout=60)
        assert ("databricks_compute.py" in log.read_text()) is bool(marker)
        assert proc.returncode == (2 if marker else 0), proc.stderr


def test_ci_refusal_is_unchanged_without_a_marker(site):
    proc = _make(site, "apply", "ENV=dev", CI="true")
    assert proc.returncode != 0
    assert "applying targets cannot run in CI" in proc.stderr
    assert REFUSED not in proc.stderr


# --- opt-in: both parts, the flag from the make command line only --------------

@pytest.fixture
def durable(tmp_path):
    state = tmp_path / "durable" / "genierails-state"
    state.mkdir(parents=True)
    return {"GENIERAILS_DURABLE_STATE_DIR": str(tmp_path / "durable"), "GENIERAILS_STATE_DIR": str(state)}


def test_opt_in_needs_both_parts(site, durable):
    flag = f"{OPT_IN}=1"
    proc = _make(site, "_guard-not-ci-apply", "ENV=dev", flag, DB_IS_JOB_CLUSTER="TRUE", **durable)
    assert proc.returncode == 0, proc.stderr
    # Opted in, the CI guard still applies as before.
    proc = _make(site, "_guard-not-ci-apply", "ENV=dev", flag, DB_IS_JOB_CLUSTER="TRUE", CI="true", **durable)
    assert proc.returncode != 0 and "applying targets cannot run in CI" in proc.stderr

    output = _assert_refused_unchanged(site, "apply", "ENV=dev", flag, DB_IS_JOB_CLUSTER="TRUE")
    assert "GENIERAILS_DURABLE_STATE_DIR" in output and "not supported end to end" in output
    output = _assert_refused_unchanged(site, "apply", "ENV=dev", DB_IS_JOB_CLUSTER="TRUE", **durable)
    assert "on the make command line" in output


@pytest.mark.parametrize("how", ["shell variable", "make -e", "value 0"])
def test_opt_in_flag_only_counts_from_the_command_line(site, durable, how):
    if how == "shell variable":
        output = _assert_refused_unchanged(site, "apply", "ENV=dev", DB_CLUSTER_ID="x", **{OPT_IN: "1"},
                                           **durable)
    elif how == "make -e":
        output = _assert_refused_unchanged(site, "-e", "apply", "ENV=dev", DB_CLUSTER_ID="x", **{OPT_IN: "1"},
                                           **durable)
    else:
        output = _assert_refused_unchanged(site, "apply", "ENV=dev", f"{OPT_IN}=0", DB_CLUSTER_ID="x",
                                           **durable)
    assert "on the make command line" in output


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/<make>/environ (Databricks is Linux)")
def test_opt_in_flag_from_an_exported_makeflags_is_refused(site, durable):
    output = _assert_refused_unchanged(site, "apply", "ENV=dev", DB_CLUSTER_ID="x", MAKEFLAGS=f"{OPT_IN}=1",
                                       **durable)
    assert "exported MAKEFLAGS" in output


@pytest.mark.parametrize("problem", ["missing", "relative", "state outside", "no state dir"])
def test_opt_in_durable_dir_must_hold_the_state_dir(site, durable, tmp_path, problem):
    env = dict(durable)
    if problem == "missing":
        env["GENIERAILS_DURABLE_STATE_DIR"] = str(tmp_path / "nope")
    elif problem == "relative":
        env["GENIERAILS_DURABLE_STATE_DIR"] = "durable"
    elif problem == "state outside":
        (tmp_path / "elsewhere").mkdir()
        env["GENIERAILS_STATE_DIR"] = str(tmp_path / "elsewhere")
    else:
        del env["GENIERAILS_STATE_DIR"]
    output = _assert_refused_unchanged(site, "apply", "ENV=dev", f"{OPT_IN}=1", DATABRICKS_APP_NAME="a", **env)
    assert "opt-in is incomplete" in output


def test_opt_in_hint_only_when_attempted():
    from shared.scripts import databricks_compute as dc

    message = dc.guard({"DATABRICKS_RUNTIME_VERSION": "15.4"}, "apply")
    assert message.startswith("ERROR: make apply refused") and "opt-in" not in message
    assert dc.guard({}, "apply", "1") is None
    assert dc.marker({"DB_CLUSTER_ID": ""}) is None


# --- the runner's second line: local-backend writes ----------------------------

@pytest.mark.parametrize("marker", MARKERS)
@pytest.mark.parametrize("command", ["apply", "destroy", "import", "state-rm"])
def test_runner_refuses_local_backend_writes_on_databricks_compute(tmp_path, marker, command):
    proc, log = _run_layer(tmp_path, command, **{marker: MARKERS[marker], "GENIERAILS_ALLOW_CI_APPLY": "1"})
    assert proc.returncode == 2
    assert "refusing terraform" in proc.stderr and "on Databricks compute" in proc.stderr
    assert not log.exists() or f"terraform {command.replace('state-', 'state ')}" not in log.read_text()
    assert not (tmp_path / "data_access" / "terraform.tfstate").exists()


def test_runner_allows_local_state_inside_the_durable_dir(tmp_path):
    proc, log = _run_layer(tmp_path, "apply", DATABRICKS_RUNTIME_VERSION="15.4",
                           GENIERAILS_DURABLE_STATE_DIR=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert "terraform apply" in log.read_text()


def test_runner_plan_is_not_refused(tmp_path):
    proc, _ = _run_layer(tmp_path, "plan", DATABRICKS_RUNTIME_VERSION="15.4")
    assert "on Databricks compute" not in proc.stderr


def _run_layer(tmp_path, command, **env):
    env_dir = tmp_path / "data_access"
    env_dir.mkdir(exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "terraform").write_text(FAKE_CLI)
    (bin_dir / "terraform").chmod(0o755)
    log = tmp_path / "calls.log"
    args = ["-auto-approve"] if command in ("apply", "destroy") else ["x.y", "id"] if command == "import" else (
        ["x.y"] if command == "state-rm" else [])
    proc = subprocess.run([str(RUNNER), "data_access", "dev", command, *args], text=True, capture_output=True,
                          timeout=60, env=_env(tmp_path, PATH=f"{bin_dir}:{os.environ['PATH']}", CALL_LOG=str(log),
                                               LAYER_ENV_DIR=str(env_dir), **env))
    return proc, log
