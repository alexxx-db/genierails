import os
import subprocess
import sys
from pathlib import Path


SHARED = Path(__file__).parents[1]
FILTER = SHARED / "scripts/filter_target_warnings.py"
USAGE_POLICY_ERROR = SHARED / "tests/fixtures/terraform_usage_policy_error.txt"
ADVICE = (
    "Enable classification in the UI, or create and attach a serverless usage policy: "
    "https://docs.databricks.com/aws/en/admin/usage/budget-policies"
)


def _run_filter(input_text: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    return subprocess.run(
        [sys.executable, "-u", str(FILTER)],
        input=input_text,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )


def test_captured_usage_policy_error_gets_actionable_advice():
    captured = USAGE_POLICY_ERROR.read_text()

    result = _run_filter(captured)

    assert result.returncode == 0
    assert result.stdout == captured + f"\n{ADVICE}\n"
    assert result.stderr == ""


def test_unrelated_error_stays_byte_for_byte_without_advice():
    error = "╷\n│ Error: cannot create catalog\n│\n│ permission denied\n╵\n"

    result = _run_filter(error)

    assert result.returncode == 0
    assert result.stdout == error
    assert ADVICE not in result.stdout
    assert result.stderr == ""


def test_filter_pipeline_preserves_upstream_failure_status():
    command = (
        f"{sys.executable!s} -c 'import sys; "
        f"sys.stdout.write(open(sys.argv[1]).read()); sys.exit(1)' "
        f"'{USAGE_POLICY_ERROR}' | {sys.executable!s} -u '{FILTER}'"
    )
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"

    result = subprocess.run(
        ["/bin/bash", "-o", "pipefail", "-c", command],
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )

    assert result.returncode != 0
    assert "Usage policy ID must not be empty" in result.stdout
    assert ADVICE in result.stdout
