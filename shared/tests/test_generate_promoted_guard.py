"""make generate refuses a deterministic promoted env (S3), before anything is written.

A promoted env's generated/ comes only from promote-to: release-plan compares
it with what promote-to produces from the commit and refuses a regenerated one
as a stale promotion, so a generate there only costs a cycle. Legacy promoted
envs have no release-plan and still generate (e.g. a genie-only prod). A config
that doesn't parse keeps generate's own handling; one that can't be read is
refused.

The refusal runs in _guarded-bootstrap, before _prepare-env, so a
never-bootstrapped env gets no scaffolding; generate_abac.py run directly
refuses the same way. The advice starts at the root of the promotion chain,
and a cycle or a missing source refuses as a malformed chain.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import release_gate
from tests.test_check_target import _snapshot
from tests.test_deterministic_check_promote import (  # noqa: F401  (repo, promoted are fixtures)
    DEPLOYER, _envs, _make, _prod_env, promoted, repo,
)

pytestmark = pytest.mark.gnu_make

DET = 'governance_mode = "deterministic"\n'


def _refusal(env, source):
    return (f"ERROR: make generate ENV={env} refused: ENV={env} is a deterministic env promoted from {source}, "
            f"so envs/{env}/generated/ comes only from promote-to")


def _assert_refused(result, env, source, tree, before):
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert _refusal(env, source) in result.stderr, out
    assert "stale promotion" in result.stderr, out
    assert f"make generate ENV={source}" in result.stderr, out
    assert f"then make promote-to ENV={env}, then commit and push envs/{env}/" in result.stderr, out
    assert f"make release-plan ENV={env} and make release ENV={env}" in result.stderr, out
    # Refused before generate starts: no banner, no generate_abac run, nothing written.
    assert "=== Generate ABAC Config" not in out, out
    assert "Traceback" not in out, out
    assert _snapshot(tree) == before


def _env(envs, name, text):
    (envs / name).mkdir(parents=True, exist_ok=True)
    (envs / name / "env.auto.tfvars").write_text(text)
    return envs / name


# ── make generate ──────────────────────────────────────────────────────────

def test_generate_refuses_a_deterministic_promoted_prod_and_writes_nothing(promoted, tmp_path):
    before = _snapshot(promoted)
    assert (_envs(promoted) / "prod" / "generated" / "abac.auto.tfvars").is_file()

    _assert_refused(_make(promoted, tmp_path, "generate", "ENV=prod"), "prod", "dev", promoted, before)


def test_generate_refuses_prod_promoted_by_name_alone(promoted, tmp_path):
    """prod without promote_from is still promoted (from dev), as env-role says."""
    env_file = _envs(promoted) / "prod" / "env.auto.tfvars"
    env_file.write_text("\n".join(line for line in env_file.read_text().splitlines()
                                  if not line.startswith("promote_from")) + "\n")
    before = _snapshot(promoted)

    _assert_refused(_make(promoted, tmp_path, "generate", "ENV=prod"), "prod", "dev", promoted, before)


@pytest.mark.parametrize("jobs", [[], ["-j4"]], ids=["serial", "make-j"])
def test_generate_refuses_a_never_bootstrapped_promoted_stg_and_writes_nothing(repo, tmp_path, jobs):
    """stg as written by hand: only env.auto.tfvars, no auth, generated/, ddl/ or data_access/."""
    stg = _env(_envs(repo), "stg", DET + 'promote_from = "dev"\ncatalog_map = { "paycat" = "spay" }\n'
               f'deployer_principal_id = "{DEPLOYER}"\n')
    before = _snapshot(repo)  # before the first call

    _assert_refused(_make(repo, tmp_path, *jobs, "generate", "ENV=stg"), "stg", "dev", repo, before)
    assert sorted(p.name for p in stg.iterdir()) == ["env.auto.tfvars"]


def test_generate_refuses_a_never_bootstrapped_promoted_prod_and_writes_nothing(repo, tmp_path):
    prod = _env(_envs(repo), "prod", _prod_env())
    before = _snapshot(repo)  # before the first call

    _assert_refused(_make(repo, tmp_path, "generate", "ENV=prod"), "prod", "dev", repo, before)
    assert sorted(p.name for p in prod.iterdir()) == ["env.auto.tfvars"]


def test_generate_names_a_promote_from_other_than_dev(promoted, tmp_path):
    envs = _envs(promoted)
    _env(envs, "stg", DET)  # a rehearsal env: the chain's root
    env_file = envs / "prod" / "env.auto.tfvars"
    env_file.write_text(env_file.read_text().replace('promote_from = "dev"', 'promote_from = "stg"'))
    before = _snapshot(promoted)

    _assert_refused(_make(promoted, tmp_path, "generate", "ENV=prod"), "prod", "stg", promoted, before)


def test_generate_starts_the_advice_where_the_promotion_chain_does(promoted, tmp_path):
    """prod from stg, stg from dev: stg can't generate either, so the advice starts in dev."""
    envs = _envs(promoted)
    _env(envs, "stg", DET + 'promote_from = "dev"\n')
    env_file = envs / "prod" / "env.auto.tfvars"
    env_file.write_text(env_file.read_text().replace('promote_from = "dev"', 'promote_from = "stg"'))
    result = _make(promoted, tmp_path, "generate", "ENV=prod")
    assert result.returncode != 0, result.stdout + result.stderr
    assert ("Change the rules in dev: make generate ENV=dev, commit, make rehearse ENV=dev, then "
            "make promote-to ENV=stg, then make promote-to ENV=prod, then") in result.stderr, result.stderr
    assert "make generate ENV=stg" not in result.stderr


def test_promote_to_still_rewrites_the_promoted_env_after_a_refused_generate(promoted, tmp_path):
    """promote-to never runs the generate target (it runs promote from the source env)."""
    assert _make(promoted, tmp_path, "generate", "ENV=prod").returncode != 0
    result = _make(promoted, tmp_path, "promote-to", "ENV=prod")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "refused" not in result.stdout + result.stderr
    check = _make(promoted, tmp_path, "promote-to", "ENV=prod", "CHECK=1")
    assert check.returncode == 0, check.stdout + check.stderr
    assert "OK: promote-to ENV=prod CHECK=1: committed files match." in check.stdout


def test_dev_generate_is_not_refused(promoted, tmp_path):
    """dev is a rehearsal env: generate runs as before (here it stops at the empty test secret)."""
    result = _make(promoted, tmp_path, "generate", "ENV=dev")
    out = result.stdout + result.stderr
    assert "=== Generate ABAC Config (dev) ===" in out, out
    assert "refused" not in out, out
    assert "fill in envs/dev/auth.auto.tfvars" in out, out


def test_legacy_promoted_prod_still_runs_generate(promoted, tmp_path):
    env_file = _envs(promoted) / "prod" / "env.auto.tfvars"
    env_file.write_text(env_file.read_text().replace('governance_mode = "deterministic"', 'governance_mode = "legacy"'))
    result = _make(promoted, tmp_path, "generate", "ENV=prod")
    out = result.stdout + result.stderr
    assert "=== Generate ABAC Config (prod) ===" in out, out
    assert "make generate ENV=prod refused" not in out, out


def test_malformed_promoted_config_keeps_generates_own_handling(promoted, tmp_path):
    env_file = _envs(promoted) / "prod" / "env.auto.tfvars"
    env_file.write_text(env_file.read_text() + '\nbroken = "x" {\n')
    result = _make(promoted, tmp_path, "generate", "ENV=prod")
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "make generate ENV=prod refused" not in out, out
    assert "generate-guard failed" not in out, out


def test_generate_guard_runs_in_guarded_bootstrap_before_prepare_env():
    """With generate a goal, generate-guard is a _guarded-bootstrap line ahead of the _bootstrap
    sub-make (like #207's guards), so it runs once, before any write, also under make -j."""
    makefile = (Path(__file__).resolve().parents[1] / "Makefile.shared").read_text()
    recipe = makefile.split("\n_guarded-bootstrap:\n", 1)[1].split("\n\n", 1)[0].splitlines()
    guard = [i for i, line in enumerate(recipe) if "generate-guard" in line]
    bootstrap = [i for i, line in enumerate(recipe) if " _bootstrap " in line]
    assert len(guard) == 1 and bootstrap and guard[0] < bootstrap[0], recipe
    assert "$(filter generate,$(MAKECMDGOALS))" in recipe[guard[0]]
    generate = makefile.split("\ngenerate:", 1)[1].split("\n\n", 1)[0]
    assert "generate-guard" not in generate


# ── generate_abac.py run directly ──────────────────────────────────────────

# Any client or SDK config built is logged, then the run stops: no live call.
FAKE_SDK = {
    "databricks/__init__.py": "",
    "databricks/sdk/useragent.py": "def with_extra(*a, **k):\n    pass\n\n\ndef with_product(*a, **k):\n    pass\n",
    "databricks/sdk/core.py": "from databricks.sdk import Config  # noqa: F401\n",
    "databricks/sdk/config.py": "from databricks.sdk import Config  # noqa: F401\n",
    "databricks/sdk/__init__.py": (
        "import os\n\n\n"
        "def _live(what):\n"
        "    with open(os.environ['FAKE_SDK_LOG'], 'a') as handle:\n"
        "        handle.write(what + '\\n')\n"
        "    raise SystemExit('fake SDK: ' + what + ' built (no live call in this test)')\n\n\n"
        "class Config:\n"
        "    def __init__(self, *a, **k):\n"
        "        _live('Config')\n\n\n"
        "class WorkspaceClient:\n"
        "    def __init__(self, *a, **k):\n"
        "        _live('WorkspaceClient')\n\n\n"
        "class AccountClient(WorkspaceClient):\n"
        "    pass\n"
    ),
}


def _full_auth(env_dir):
    (env_dir / "auth.auto.tfvars").write_text(
        'databricks_account_id = ""\ndatabricks_workspace_id = "1"\n'
        'databricks_workspace_host = "https://unreachable.invalid"\n'
        f'databricks_client_id = "{DEPLOYER}"\ndatabricks_client_secret = "test-only-secret"\n')


def _run_generator_directly(repo, tmp_path, env):
    sdk = tmp_path / "fake_sdk"
    for rel, text in FAKE_SDK.items():
        (sdk / rel).parent.mkdir(parents=True, exist_ok=True)
        (sdk / rel).write_text(text)
    log = tmp_path / "sdk.log"
    environ = {k: v for k, v in os.environ.items() if not k.startswith("DATABRICKS_")}
    environ.update(PYTHONPATH=os.pathsep.join([str(sdk), environ.get("PYTHONPATH", "")]),
                   FAKE_SDK_LOG=str(log), PIP_NO_INDEX="1", PIP_REQUIRE_VIRTUALENV="1",
                   PYTHONDONTWRITEBYTECODE="1")
    # The repro: cd aws/envs/<env>; python ../../../shared/generate_abac.py
    result = subprocess.run([sys.executable, "../../../shared/generate_abac.py"], cwd=_envs(repo) / env,
                            text=True, capture_output=True, env=environ, timeout=300)
    return result, (log.read_text().split() if log.exists() else [])


def test_generate_abac_run_directly_refuses_a_promoted_prod_and_builds_no_client(promoted, tmp_path):
    _full_auth(_envs(promoted) / "prod")
    before = _snapshot(promoted)

    result, live = _run_generator_directly(promoted, tmp_path, "prod")
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert _refusal("prod", "dev") in result.stderr, out
    assert "ABAC Configuration Generator" not in out, out
    assert live == [], out
    assert _snapshot(promoted) == before


def test_generate_abac_run_directly_still_reaches_the_sdk_in_a_legacy_promoted_prod(promoted, tmp_path):
    """The control for the test above: the same run in a legacy prod does build a client."""
    prod = _envs(promoted) / "prod"
    _full_auth(prod)
    env_file = prod / "env.auto.tfvars"
    env_file.write_text(env_file.read_text().replace('governance_mode = "deterministic"', 'governance_mode = "legacy"'))

    result, live = _run_generator_directly(promoted, tmp_path, "prod")
    out = result.stdout + result.stderr
    assert "make generate ENV=prod refused" not in out, out
    assert live, out


# ── promotion chains (generate-guard on its own) ───────────────────────────

def _guard(envs, name, capsys):
    rc = release_gate.main(["generate-guard", "--env-dir", str(envs / name), "--env", name])
    return rc, capsys.readouterr().err


def test_a_chain_of_twelve_hops_starts_the_advice_at_its_root(tmp_path, capsys):
    envs = tmp_path / "envs"
    _env(envs, "e0", DET)
    for hop in range(1, 13):
        _env(envs, f"e{hop}", DET + f'promote_from = "e{hop - 1}"\n')

    rc, err = _guard(envs, "e12", capsys)
    assert rc == 1, err
    assert "Change the rules in e0: make generate ENV=e0, commit, make rehearse ENV=e0, then " in err, err
    assert ", then ".join(f"make promote-to ENV=e{hop}" for hop in range(1, 13)) in err, err
    assert "malformed" not in err


def test_a_two_env_cycle_refuses_as_a_malformed_chain_with_no_advice(tmp_path, capsys):
    envs = tmp_path / "envs"
    _env(envs, "stg", DET + 'promote_from = "prod"\n')
    _env(envs, "prod", DET + 'promote_from = "stg"\n')

    rc, err = _guard(envs, "prod", capsys)
    assert rc == 1, err
    assert "make generate ENV=prod refused: malformed promotion chain prod -> stg -> prod: a cycle" in err, err
    assert "make promote-to" not in err and "make generate ENV=stg" not in err and "Change the rules" not in err


def test_a_self_cycle_refuses_as_a_malformed_chain(tmp_path, capsys):
    envs = tmp_path / "envs"
    _env(envs, "stg", DET + 'promote_from = "stg"\n')

    rc, err = _guard(envs, "stg", capsys)
    assert rc == 1, err
    assert "malformed promotion chain stg -> stg: a cycle" in err, err


@pytest.mark.parametrize("prod_text, detail", [
    (DET + 'promote_from = "qa"\n',
     "envs/prod/env.auto.tfvars promotes from qa, and envs/qa/env.auto.tfvars doesn't exist"),
    (DET, "envs/prod/env.auto.tfvars promotes from dev, and envs/dev/env.auto.tfvars doesn't exist"),
    (DET + 'promote_from = "../dev"\n', "promote_from = '../dev' in envs/prod/env.auto.tfvars is not an env name"),
], ids=["missing-named-source", "missing-default-dev", "not-an-env-name"])
def test_a_missing_or_unusable_source_refuses_as_a_malformed_chain(tmp_path, capsys, prod_text, detail):
    envs = tmp_path / "envs"
    _env(envs, "prod", prod_text)

    rc, err = _guard(envs, "prod", capsys)
    assert rc == 1, err
    assert "make generate ENV=prod refused: malformed promotion chain" in err, err
    assert detail in err, err
    assert "make promote-to" not in err and "Change the rules" not in err


def test_a_missing_source_further_up_the_chain_names_where_it_breaks(tmp_path, capsys):
    envs = tmp_path / "envs"
    _env(envs, "stg", DET + 'promote_from = "qa"\n')
    _env(envs, "prod", DET + 'promote_from = "stg"\n')

    rc, err = _guard(envs, "prod", capsys)
    assert rc == 1, err
    assert ("malformed promotion chain prod -> stg -> qa: envs/stg/env.auto.tfvars promotes from qa, "
            "and envs/qa/env.auto.tfvars doesn't exist") in err, err


def test_an_unparseable_env_up_the_chain_refuses_as_a_malformed_chain(tmp_path, capsys):
    envs = tmp_path / "envs"
    _env(envs, "dev", DET)
    _env(envs, "stg", 'broken = "x" {\n')
    _env(envs, "prod", DET + 'promote_from = "stg"\n')

    rc, err = _guard(envs, "prod", capsys)
    assert rc == 1, err
    assert "malformed promotion chain prod -> stg: envs/stg/env.auto.tfvars can't be read" in err, err


def test_a_legacy_promoted_source_is_the_root(tmp_path, capsys):
    """A legacy env still generates, so the advice can start there."""
    envs = tmp_path / "envs"
    _env(envs, "dev", DET)
    _env(envs, "stg", 'governance_mode = "legacy"\npromote_from = "dev"\n')
    _env(envs, "prod", DET + 'promote_from = "stg"\n')

    rc, err = _guard(envs, "prod", capsys)
    assert rc == 1, err
    assert "Change the rules in stg: make generate ENV=stg" in err, err


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_env_file_refuses_fail_closed(tmp_path, capsys):
    envs = tmp_path / "envs"
    env_file = _env(envs, "prod", DET) / "env.auto.tfvars"
    env_file.chmod(0)
    try:
        rc, err = _guard(envs, "prod", capsys)
    finally:
        env_file.chmod(0o644)
    assert rc == 1, err
    assert "make generate ENV=prod refused: can't read" in err, err


@pytest.mark.parametrize("name, text, refused", [
    ("prod", DET, True),
    ("stg", DET + 'promote_from = "dev"\n', True),
    ("stg", DET, False),
    ("dev", DET, False),
    ("prod", 'governance_mode = "legacy"\npromote_from = "dev"\n', False),
    ("prod", "", False),
    ("prod", DET + 'broken = "x" {\n', False),
], ids=["det-prod", "det-promoted-stg", "det-rehearsal-stg", "det-dev", "legacy-prod", "no-config", "malformed"])
def test_generate_guard_cli(tmp_path, capsys, name, text, refused):
    envs = tmp_path / "envs"
    if name != "dev":
        _env(envs, "dev", DET)
    (envs / name).mkdir(parents=True, exist_ok=True)
    if text:
        (envs / name / "env.auto.tfvars").write_text(text)
    rc, err = _guard(envs, name, capsys)
    assert rc == (1 if refused else 0), err
    assert (f"make generate ENV={name} refused" in err) is refused
    if refused:
        assert _refusal(name, "dev") in err, err
    else:
        assert err == ""
