"""promote-to REUSE_SOURCE_AUTH=1: fill a blank destination auth.auto.tfvars from a same-SP source.

Only an explicit command-line opt-in acts, only on a destination still in its
template state, copying only the account ID, client ID and secret; the
workspace host and ID stay the destination's. A refused promotion restores the
file, and no credential value is ever printed. Every auth file here is a temp
fixture; terraform is a stub and nothing reaches Databricks.
"""

import os
import subprocess
import sys
from pathlib import Path

import hcl2
import pytest

from tests.test_deterministic_check_promote import (  # noqa: F401  (repo is a fixture)
    DEPLOYER, PROD_DEPLOYER, _break_account, _envs, _make, _prod_env, _state, repo,
)

SHARED = Path(__file__).resolve().parents[1]
SCRIPT = SHARED / "scripts" / "prepare_destination_auth.py"
TEMPLATE = (SHARED / "auth.auto.tfvars.example").read_text()
SECRET = "dose0123SECRETvalue4567"
DEV_HOST = "https://dev-workspace.invalid"
PROD_HOST = "https://prod-workspace.invalid"
OLD_HINT = "Next: edit {}/auth.auto.tfvars  (prod workspace credentials)"


def _source_auth(secret=SECRET, client_id=DEPLOYER, host=DEV_HOST):
    return (f'databricks_account_id = "acct-1"\ndatabricks_client_id = "{client_id}"\n'
            f'databricks_client_secret = "{secret}"\ndatabricks_workspace_id = "111"\n'
            f'databricks_workspace_host = "{host}"\n')


def _run(tmp_path, *args, source=None, dest=None, env_file=None):
    source_path = tmp_path / "src.auth.auto.tfvars"
    dest_path = tmp_path / "dest" / "auth.auto.tfvars"
    dest_path.parent.mkdir(exist_ok=True)
    source_path.write_text(_source_auth() if source is None else source)
    if dest is not None:
        dest_path.write_text(dest)
    elif not dest_path.exists():
        dest_path.write_text(TEMPLATE)
    flags = ["--source", str(source_path), "--dest", str(dest_path)]
    if env_file is not None:
        (tmp_path / "env.auto.tfvars").write_text(env_file)
        flags += ["--dest-env-file", str(tmp_path / "env.auto.tfvars")]
    result = subprocess.run([sys.executable, str(SCRIPT), *flags, *args], text=True, capture_output=True)
    return result, dest_path


def _no_secret(result, secret=SECRET):
    out = result.stdout + result.stderr
    assert secret not in out, out
    assert "Traceback" not in out, out


def test_blank_template_is_filled_with_source_credentials_and_destination_workspace(tmp_path):
    result, dest = _run(tmp_path, "--reuse", "1", "--workspace-host", PROD_HOST, "--workspace-id", "222")

    assert result.returncode == 0, result.stderr
    assert hcl2.loads(dest.read_text()) == {
        "databricks_account_id": "acct-1", "databricks_client_id": DEPLOYER,
        "databricks_client_secret": SECRET, "databricks_workspace_id": "222",
        "databricks_workspace_host": PROD_HOST,
    }
    assert DEV_HOST not in dest.read_text()
    assert dest.read_text().startswith("# Databricks Authentication")  # the template's comments are kept
    assert dest.stat().st_mode & 0o777 == 0o600
    assert "filled" in result.stdout and PROD_HOST in result.stdout
    _no_secret(result)


def test_workspace_host_and_id_already_in_the_destination_are_kept(tmp_path):
    dest = TEMPLATE.replace('databricks_workspace_host = ""', f'databricks_workspace_host = "{PROD_HOST}"') \
                   .replace('databricks_workspace_id  = ""', 'databricks_workspace_id  = "222"')

    result, path = _run(tmp_path, "--reuse", "1", dest=dest)

    assert result.returncode == 0, result.stderr
    config = hcl2.loads(path.read_text())
    assert (config["databricks_workspace_host"], config["databricks_workspace_id"]) == (PROD_HOST, "222")
    assert config["databricks_client_secret"] == SECRET


def test_account_host_is_carried_for_azure(tmp_path):
    source = _source_auth() + 'databricks_account_host = "https://accounts.azuredatabricks.net"\n'

    result, path = _run(tmp_path, "--reuse", "1", "--workspace-host", PROD_HOST, "--workspace-id", "222",
                        source=source)

    assert result.returncode == 0, result.stderr
    assert hcl2.loads(path.read_text())["databricks_account_host"] == "https://accounts.azuredatabricks.net"


@pytest.mark.parametrize("args, message", [
    ((), "needs the destination's databricks_workspace_host: pass WORKSPACE_HOST="),
    (("--workspace-host", PROD_HOST), "needs the destination's databricks_workspace_id: pass WORKSPACE_ID="),
    (("--workspace-host", DEV_HOST + "/", "--workspace-id", "222"), "databricks_workspace_host is the source env's"),
    (("--workspace-host", PROD_HOST, "--workspace-id", "111"), "databricks_workspace_id is the source env's"),
    (("--workspace-host", "prod.invalid", "--workspace-id", "222"), "must be an https:// workspace URL"),
    (("--workspace-host", PROD_HOST, "--workspace-id", "abc"), "must be the numeric workspace ID"),
])
def test_destination_workspace_is_required_and_never_the_source(tmp_path, args, message):
    result, dest = _run(tmp_path, "--reuse", "1", *args)

    assert result.returncode == 1
    assert message in result.stderr, result.stderr
    assert dest.read_text() == TEMPLATE
    _no_secret(result)


def test_operator_host_that_contradicts_the_destination_file_is_refused(tmp_path):
    dest = TEMPLATE.replace('databricks_workspace_host = ""', f'databricks_workspace_host = "{PROD_HOST}"')

    result, path = _run(tmp_path, "--reuse", "1", "--workspace-host", "https://other.invalid",
                        "--workspace-id", "222", dest=dest)

    assert result.returncode == 1
    assert "WORKSPACE_HOST differs from databricks_workspace_host" in result.stderr
    assert path.read_text() == dest


@pytest.mark.parametrize("dest", [
    _source_auth(secret="prod-secret", host=PROD_HOST),
    TEMPLATE.replace('databricks_client_id     = ""', 'databricks_client_id     = "typed-by-hand"'),
    TEMPLATE.replace('databricks_client_secret = ""', 'databricks_client_secret = "only-secret"'),
    TEMPLATE + 'databricks_account_host = "https://accounts.azuredatabricks.net"\n',
], ids=["configured", "client-id-only", "secret-only", "extra-key"])
def test_configured_destination_is_never_overwritten(tmp_path, dest):
    result, path = _run(tmp_path, "--reuse", "1", "--workspace-host", PROD_HOST, "--workspace-id", "222", dest=dest)

    assert result.returncode == 0, result.stderr
    assert "left unchanged" in result.stdout
    assert path.read_text() == dest
    _no_secret(result)


def test_symlinked_destination_is_left_alone(tmp_path):
    target = tmp_path / "elsewhere.tfvars"
    target.write_text(TEMPLATE)
    (tmp_path / "dest").mkdir()
    (tmp_path / "dest" / "auth.auto.tfvars").symlink_to(target)

    result, path = _run(tmp_path, "--reuse", "1", "--workspace-host", PROD_HOST, "--workspace-id", "222")

    assert result.returncode == 0 and "left unchanged" in result.stdout
    assert path.is_symlink() and target.read_text() == TEMPLATE


@pytest.mark.parametrize("reuse, message", [("", "only apply with REUSE_SOURCE_AUTH=1"),
                                            ("yes", "REUSE_SOURCE_AUTH takes only 1")])
def test_only_an_explicit_reuse_1_acts(tmp_path, reuse, message):
    result, dest = _run(tmp_path, "--reuse", reuse, "--workspace-host", PROD_HOST, "--workspace-id", "222")

    assert result.returncode == 1 and message in result.stderr
    assert dest.read_text() == TEMPLATE


def test_different_committed_deployer_means_not_the_same_sp(tmp_path):
    result, dest = _run(tmp_path, "--reuse", "1", "--workspace-host", PROD_HOST, "--workspace-id", "222",
                        env_file=f'deployer_principal_id = "{PROD_DEPLOYER}"\n')

    assert result.returncode == 1
    assert "do not share one service principal" in result.stderr
    assert dest.read_text() == TEMPLATE
    _no_secret(result)


def test_placeholder_deployer_skips_the_same_sp_check(tmp_path):
    result, _ = _run(tmp_path, "--reuse", "1", "--workspace-host", PROD_HOST, "--workspace-id", "222",
                     env_file='deployer_principal_id = "<your-deployer-principal-id>"\n')

    assert result.returncode == 0, result.stderr


def test_unconfigured_source_is_refused(tmp_path):
    result, dest = _run(tmp_path, "--reuse", "1", "--workspace-host", PROD_HOST, "--workspace-id", "222",
                        source=TEMPLATE)

    assert result.returncode == 1 and "has no client ID and secret to reuse" in result.stderr
    assert dest.read_text() == TEMPLATE


@pytest.mark.parametrize("secret", ['with"quote', "back\\slash"])
def test_secret_with_hcl_escapes_round_trips(tmp_path, secret):
    escaped = secret.replace("\\", "\\\\").replace('"', '\\"')
    result, dest = _run(tmp_path, "--reuse", "1", "--workspace-host", PROD_HOST, "--workspace-id", "222",
                        source=_source_auth(secret=escaped))

    assert result.returncode == 0, result.stderr
    assert hcl2.loads(dest.read_text())["databricks_client_secret"] == secret
    _no_secret(result, secret)


def test_secret_with_a_template_sequence_is_refused_not_mangled(tmp_path):
    secret = "abc$${x}SECRETdef"
    result, dest = _run(tmp_path, "--reuse", "1", "--workspace-host", PROD_HOST, "--workspace-id", "222",
                        source=_source_auth(secret=secret))

    assert result.returncode == 1 and "fill the destination auth.auto.tfvars by hand" in result.stderr
    assert dest.read_text() == TEMPLATE
    _no_secret(result, "SECRETdef")



@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unexpected_failure_prints_a_fixed_message_and_no_traceback(tmp_path):
    (tmp_path / "dest").mkdir()
    (tmp_path / "dest" / "auth.auto.tfvars").write_text(TEMPLATE)
    (tmp_path / "dest").chmod(0o555)  # the atomic write cannot create its temp file
    try:
        result, dest = _run(tmp_path, "--reuse", "1", "--workspace-host", PROD_HOST, "--workspace-id", "222")
    finally:
        (tmp_path / "dest").chmod(0o755)

    assert result.returncode == 1
    assert "could not prepare the destination auth.auto.tfvars; nothing was written." in result.stderr
    assert dest.read_text() == TEMPLATE
    _no_secret(result)


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_source_that_is_not_a_regular_file_is_refused(tmp_path, kind):
    real = tmp_path / "real.tfvars"
    real.write_text(_source_auth())
    source = tmp_path / "linked.auth.auto.tfvars"
    if kind == "symlink":
        source.symlink_to(real)
    elif kind == "directory":
        source.mkdir()
    else:
        os.mkfifo(source)  # O_NONBLOCK: opening it must not hang
    dest = tmp_path / "dest" / "auth.auto.tfvars"
    dest.parent.mkdir()
    dest.write_text(TEMPLATE)

    result = subprocess.run([sys.executable, str(SCRIPT), "--source", str(source), "--dest", str(dest), "--reuse", "1",
                             "--workspace-host", PROD_HOST, "--workspace-id", "222"],
                            text=True, capture_output=True, timeout=30)

    assert result.returncode == 1
    assert "is not a regular file" in result.stderr, result.stderr
    assert dest.read_text() == TEMPLATE
    _no_secret(result)


def _swap_configured(dest):
    fresh = dest.with_name("fresh.tfvars")
    fresh.write_text(_source_auth(secret="typed-meanwhile", host=PROD_HOST))
    os.replace(fresh, dest)


def _edit_in_place(dest):
    with dest.open("r+") as handle:  # same inode, new bytes
        handle.write(_source_auth(secret="typed-meanwhile", host=PROD_HOST))


def _swap_symlink(dest):
    target = dest.with_name("elsewhere.tfvars")
    target.write_text(TEMPLATE)
    dest.unlink()
    dest.symlink_to(target)


@pytest.mark.parametrize("swap", [_swap_configured, _edit_in_place, _swap_symlink],
                         ids=["new-configured-file", "edited-in-place", "symlink"])
def test_destination_swapped_after_validation_is_not_overwritten(tmp_path, monkeypatch, swap):
    sys.path.insert(0, str(SHARED / "scripts"))
    import prepare_destination_auth as module
    source = tmp_path / "src.auth.auto.tfvars"
    source.write_text(_source_auth())
    dest = tmp_path / "dest" / "auth.auto.tfvars"
    dest.parent.mkdir()
    dest.write_text(TEMPLATE)
    render = module._render
    swapped = []

    def render_then_swap(text, values):
        rendered = render(text, values)
        swap(dest)  # between the checks and the replace
        swapped.append(_contents(dest))
        return rendered

    monkeypatch.setattr(module, "_render", render_then_swap)

    with pytest.raises(module.AuthError, match="changed while it was being prepared"):
        module.prepare(source, dest, None, PROD_HOST, "222")

    assert swapped == [_contents(dest)]
    assert SECRET not in dest.read_text()
    assert not list(dest.parent.glob(".auth.*.tmp"))


def _contents(path):
    return ("link", os.readlink(path)) if path.is_symlink() else ("file", path.read_text())


def test_opt_in_outside_any_make_command_line_is_refused(tmp_path):
    result, dest = _run(tmp_path, "--reuse", "1", "--workspace-host", PROD_HOST, "--workspace-id", "222",
                        "--require-make-command-line")

    assert result.returncode == 1
    assert "REUSE_SOURCE_AUTH=1 is not on the make command line" in result.stderr
    assert dest.read_text() == TEMPLATE

# Malformed source (or destination) files whose broken token is the secret: the
# parser's text would echo it, so the message must be the fixed one.
MALFORMED = {
    "unquoted": f"databricks_client_id = \"{DEPLOYER}\"\ndatabricks_client_secret = {SECRET} $$\n",
    "unterminated": f"databricks_client_id = \"{DEPLOYER}\"\ndatabricks_client_secret = \"{SECRET}\n",
    "non-utf8": None,
}


@pytest.mark.parametrize("which", ["source", "dest"])
@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_malformed_auth_never_prints_the_secret(tmp_path, which, case):
    text = MALFORMED[case]
    path = tmp_path / ("src.auth.auto.tfvars" if which == "source" else "dest/auth.auto.tfvars")
    path.parent.mkdir(exist_ok=True)
    if text is None:
        path.write_bytes(b'databricks_client_secret = "' + SECRET.encode() + b'\xff\xfe"\n')
    else:
        path.write_text(text)
    before = path.read_bytes()
    if which == "source":
        result, dest = _run_with_source_file(tmp_path, path)
    else:
        result, dest = _run(tmp_path, "--reuse", "1", "--workspace-host", PROD_HOST, "--workspace-id", "222")

    assert result.returncode == 1
    assert "is not valid HCL; nothing was written." in result.stderr, result.stderr
    _no_secret(result)
    assert path.read_bytes() == before
    if which == "source":
        assert dest.read_text() == TEMPLATE


def _run_with_source_file(tmp_path, source):
    dest = tmp_path / "dest" / "auth.auto.tfvars"
    dest.parent.mkdir(exist_ok=True)
    dest.write_text(TEMPLATE)
    result = subprocess.run([sys.executable, str(SCRIPT), "--source", str(source), "--dest", str(dest), "--reuse", "1",
                             "--workspace-host", PROD_HOST, "--workspace-id", "222"], text=True, capture_output=True)
    return result, dest


# --- make promote-to end to end -------------------------------------------------------------------


def _prod(repo, deployer=DEPLOYER):
    """A prod env set up by hand, without an auth file: promote-to scaffolds the template."""
    prod = _envs(repo) / "prod"
    prod.mkdir()
    (prod / "env.auto.tfvars").write_text(_prod_env(deployer))
    (_envs(repo) / "dev" / "auth.auto.tfvars").write_text(_source_auth())
    return prod


def _reuse(*extra):
    return ("promote-to", "ENV=prod", "REUSE_SOURCE_AUTH=1", f"WORKSPACE_HOST={PROD_HOST}", "WORKSPACE_ID=222", *extra)


def test_promote_to_with_opt_in_fills_prod_auth(repo, tmp_path):
    prod = _prod(repo)

    result = _make(repo, tmp_path, *_reuse())

    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert hcl2.load((prod / "auth.auto.tfvars").open()) == {
        "databricks_account_id": "acct-1", "databricks_client_id": DEPLOYER,
        "databricks_client_secret": SECRET, "databricks_workspace_id": "222",
        "databricks_workspace_host": PROD_HOST,
    }
    assert DEV_HOST not in (prod / "auth.auto.tfvars").read_text()
    assert "REUSE_SOURCE_AUTH: filled" in out
    assert "Next: edit" not in out
    assert SECRET not in out


def test_promote_to_without_opt_in_leaves_the_template_and_prints_the_hint(repo, tmp_path):
    prod = _prod(repo)

    result = _make(repo, tmp_path, "promote-to", "ENV=prod", REUSE_SOURCE_AUTH="1",
                   WORKSPACE_HOST=PROD_HOST, WORKSPACE_ID="222")  # the shell's, not the command line's

    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert (prod / "auth.auto.tfvars").read_text() == TEMPLATE
    assert OLD_HINT.format(prod) in out, out
    assert "REUSE_SOURCE_AUTH=1 WORKSPACE_HOST=<prod host> WORKSPACE_ID=<prod id>" in out
    assert "REUSE_SOURCE_AUTH:" not in out


def test_promote_to_never_overwrites_a_configured_prod_auth(repo, tmp_path):
    prod = _prod(repo)
    configured = _source_auth(secret="prod-own-secret", host=PROD_HOST)
    (prod / "auth.auto.tfvars").write_text(configured)

    result = _make(repo, tmp_path, *_reuse())

    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert (prod / "auth.auto.tfvars").read_text() == configured
    assert "left unchanged" in out
    assert SECRET not in out and "prod-own-secret" not in out


def test_refused_promotion_restores_prod_auth(repo, tmp_path):
    prod = _prod(repo)
    (prod / "auth.auto.tfvars").write_text(TEMPLATE)
    _break_account(repo)  # the split refuses after the auth file was filled
    before = _state(repo)

    result = _make(repo, tmp_path, *_reuse())

    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "REUSE_SOURCE_AUTH: filled" in out
    assert "Refused: nothing was changed" in out, out
    assert (prod / "auth.auto.tfvars").read_text() == TEMPLATE
    assert _state(repo) == before
    assert SECRET not in out


def test_promote_to_refuses_reuse_for_a_different_prod_deployer(repo, tmp_path):
    prod = _prod(repo, deployer=PROD_DEPLOYER)
    (prod / "auth.auto.tfvars").write_text(TEMPLATE)
    before = _state(repo)

    result = _make(repo, tmp_path, *_reuse())

    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert "do not share one service principal" in out, out
    assert _state(repo) == before
    assert SECRET not in out


def test_shell_opt_in_is_ignored_even_with_command_line_workspace(repo, tmp_path):
    prod = _prod(repo)

    result = _make(repo, tmp_path, "promote-to", "ENV=prod", f"WORKSPACE_HOST={PROD_HOST}", "WORKSPACE_ID=222",
                   REUSE_SOURCE_AUTH="1")

    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert (prod / "auth.auto.tfvars").read_text() == TEMPLATE
    assert OLD_HINT.format(prod) in out, out
    assert "REUSE_SOURCE_AUTH:" not in out


def test_opt_in_from_an_exported_makeflags_is_refused(repo, tmp_path):
    prod = _prod(repo)
    (prod / "auth.auto.tfvars").write_text(TEMPLATE)
    before = _state(repo)

    result = _make(repo, tmp_path, "promote-to", "ENV=prod", f"WORKSPACE_HOST={PROD_HOST}", "WORKSPACE_ID=222",
                   MAKEFLAGS="REUSE_SOURCE_AUTH=1")

    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "REUSE_SOURCE_AUTH=1 is not on the make command line" in out, out
    assert "Refused: nothing was changed" in out, out
    assert _state(repo) == before
    assert SECRET not in out
