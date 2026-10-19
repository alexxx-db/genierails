"""PR #183 follow-ups N1–N3: auth parse secrecy, dangling auth symlinks, DATA_ACCESS_SUBDIR."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

SHARED = Path(__file__).resolve().parents[1]
SCRIPTS = SHARED / "scripts"
for _path in (SHARED, SCRIPTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts import release_gate as rg  # noqa: E402

# Marker that must never appear in any parser-facing output.
SECRET_FRAGMENT = "!Sekret$Value"
MALFORMED_AUTH = (
    'databricks_account_id = "123"\n'
    'databricks_workspace_host = "https://workspace.example"\n'
    f"databricks_client_secret = Zq9{SECRET_FRAGMENT}\n"
)


def _write_malformed(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(MALFORMED_AUTH)
    return path


def _assert_no_secret(text: str) -> None:
    assert SECRET_FRAGMENT not in text
    assert "Sekret" not in text


# -- N1: malformed auth never echoes the secret --------------------------------

def test_account_auth_endpoint_hash_refuses_malformed_auth_without_secret(tmp_path, capsys):
    env_dir = tmp_path / "envs" / "account"
    auth = _write_malformed(env_dir / "auth.auto.tfvars")
    with pytest.raises(rg.Refused, match="is not valid HCL") as raised:
        rg.account_input_hashes(env_dir)
    _assert_no_secret(str(raised.value))
    _assert_no_secret(capsys.readouterr().out + capsys.readouterr().err)
    # CLI path uses the same gate (generic Exception handler must not fire).
    rc = rg.main(["account-tag-sync-diff", "--env-dir", str(env_dir), "--env", "account",
                  "--out", str(tmp_path / "out.json")])
    captured = capsys.readouterr()
    assert rc == 1
    assert "is not valid HCL" in captured.err
    _assert_no_secret(captured.out + captured.err)
    assert auth.is_file()


def test_sync_tag_policies_load_auth_refuses_malformed_without_secret(tmp_path):
    _write_malformed(tmp_path / "auth.auto.tfvars")
    (tmp_path / "abac.auto.tfvars").write_text("tag_policies = [{ key = \"k\", values = [\"v\"] }]\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "sync_tag_policies.py"), "--dry-run", "--json",
         str(tmp_path / "abac.auto.tfvars")],
        cwd=tmp_path, text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "is not valid HCL" in combined
    _assert_no_secret(combined)


def test_wait_for_tag_policies_load_auth_refuses_malformed_without_secret(tmp_path):
    _write_malformed(tmp_path / "auth.auto.tfvars")
    result = subprocess.run(
        [sys.executable, "-c",
         "from pathlib import Path; import sys; "
         f"sys.path.insert(0, {str(SCRIPTS)!r}); "
         "import wait_for_tag_policies as w; "
         f"w.WORK_DIR = Path({str(tmp_path)!r}); w._load_auth()"],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "is not valid HCL" in combined
    _assert_no_secret(combined)


def test_auth_configured_malformed_is_false_without_raising(tmp_path):
    from auth_configured import auth_configured

    auth = _write_malformed(tmp_path / "auth.auto.tfvars")
    assert auth_configured(auth) is False


def test_generate_abac_credentials_parse_omits_secret(tmp_path, capsys):
    import generate_abac

    auth = _write_malformed(tmp_path / "auth.auto.tfvars")
    cfg = generate_abac._load_tfvars(auth, "credentials")
    assert cfg == {}
    captured = capsys.readouterr()
    assert "not valid HCL" in captured.out
    _assert_no_secret(captured.out + captured.err)


def test_verify_load_auth_refuses_malformed_without_secret(tmp_path):
    import verify_effective_access as vea

    auth = _write_malformed(tmp_path / "auth.auto.tfvars")
    with pytest.raises(ValueError, match="is not valid HCL") as raised:
        vea.load_auth(auth)
    _assert_no_secret(str(raised.value))


def test_custom_masks_workspace_client_refuses_malformed_without_secret(tmp_path):
    from scripts import custom_masks as cm

    auth = _write_malformed(tmp_path / "auth.auto.tfvars")
    with pytest.raises(cm.Refused, match="is not valid HCL") as raised:
        cm.workspace_client(auth)
    _assert_no_secret(str(raised.value))


def test_footprint_load_hcl_auth_omits_secret(tmp_path):
    from scripts.footprint import FootprintError, load_hcl

    auth = _write_malformed(tmp_path / "auth.auto.tfvars")
    with pytest.raises(FootprintError, match="not valid HCL") as raised:
        load_hcl(auth)
    _assert_no_secret(str(raised.value))


def test_audit_schema_drift_auth_omits_secret(tmp_path):
    from scripts import audit_schema_drift as asd

    auth = _write_malformed(tmp_path / "auth.auto.tfvars")
    with pytest.raises(RuntimeError, match="not valid HCL") as raised:
        asd._load_hcl(auth)
    _assert_no_secret(str(raised.value))


# -- N2: dangling auth symlink is refused, never written through ---------------

@pytest.mark.skipif(__import__("shutil").which("make") is None, reason="make not installed")
def test_prepare_env_refuses_dangling_auth_symlink(tmp_path):
    REPO = SHARED.parent
    CLOUD_DIR_LINE = next(
        line for line in (REPO / "aws/Makefile").read_text().splitlines()
        if line.startswith("override GENIERAILS_CLOUD_DIR"))
    root = tmp_path / "cloud"
    (root / "envs").mkdir(parents=True)
    (root / "Makefile").write_text(
        f"{CLOUD_DIR_LINE}\nSHARED_ROOT := {SHARED}\nCLOUD_ROOT := {root}\nCLOUD := aws\ninclude {SHARED / 'Makefile.shared'}\n")
    env_dir = root / "envs" / "dev"
    env_dir.mkdir(parents=True)
    dangling = env_dir / "auth.auto.tfvars"
    dangling.symlink_to("missing-target.auto.tfvars")
    victim = env_dir / "missing-target.auto.tfvars"
    assert not victim.exists()

    env = {k: v for k, v in os.environ.items()
           if k not in ("MAKEFLAGS", "MAKELEVEL", "MAKEFILES", "GENIERAILS_CLOUD_DIR", "ENV", "ENV_DIR")}
    result = subprocess.run(
        ["make", "--no-print-directory", "setup", "ENV=dev"],
        cwd=root, text=True, capture_output=True, timeout=120, env=env,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "dangling symlink" in (result.stdout + result.stderr)
    assert dangling.is_symlink()
    assert not victim.exists(), "setup must not write through a dangling auth symlink"


# -- N3: DATA_ACCESS_SUBDIR override -------------------------------------------

def test_data_access_subdir_override_used_for_live_policies_path(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ACCESS_SUBDIR", "gov_layer")
    assert rg.data_access_subdir() == "gov_layer"
    assert rg.runtime_dirs() == {"gov_layer"}
    env_dir = tmp_path / "envs" / "dev"
    auth = env_dir / "gov_layer" / "auth.auto.tfvars"
    auth.parent.mkdir(parents=True)
    auth.write_text('databricks_workspace_host = "https://example.invalid"\n')
    seen: list[Path] = []

    def fake_lister(path: Path):
        seen.append(Path(path))
        return lambda _catalog: set()

    import remap_generated_config
    monkeypatch.setattr(remap_generated_config, "live_policy_lister", fake_lister)
    rg.live_policies(env_dir, ["cat"])
    assert seen == [auth]


def test_data_access_subdir_override_skips_reserved_env_name(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ACCESS_SUBDIR", "gov_layer")
    account = tmp_path / "envs" / "account"
    account.mkdir(parents=True)
    (tmp_path / "envs" / "gov_layer").mkdir()
    (tmp_path / "envs" / "gov_layer" / "env.auto.tfvars").write_text(
        'governance_mode = "deterministic"\n')
    (tmp_path / "envs" / "dev").mkdir()
    (tmp_path / "envs" / "dev" / "env.auto.tfvars").write_text(
        'governance_mode = "legacy"\n')
    assert rg.any_deterministic_env(account) is False
