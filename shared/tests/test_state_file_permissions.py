from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from scripts.state_file_permissions import tighten_state_files


ROOT = Path(__file__).parents[2]
RUNNER = ROOT / "shared" / "scripts" / "terraform_layer.sh"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_tightens_existing_state_and_warns(tmp_path, capsys):
    for name in ("terraform.tfstate", "terraform.tfstate.backup", "terraform.tfstate.123.backup"):
        path = tmp_path / name
        path.write_text("{}")
        path.chmod(0o644)

    tighten_state_files(tmp_path)

    assert all(_mode(path) == 0o600 for path in tmp_path.glob("terraform.tfstate*"))
    err = capsys.readouterr().err
    assert err.count("tightening to 0600") == 3
    assert "may hold secrets" in err


def test_runner_creates_account_and_env_state_as_0600_with_open_umask(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    terraform = fake_bin / "terraform"
    terraform.write_text("""#!/bin/sh
set -eu
if [ "$1" = init ]; then exit 0; fi
state="$LAYER_ENV_DIR/terraform.tfstate"
printf '{}' > "$state"
""")
    terraform.chmod(0o755)
    envs = tmp_path / "envs"
    account = envs / "account"
    workspace = envs / "dev"
    account.mkdir(parents=True)
    workspace.mkdir()
    env = os.environ | {"PATH": f"{fake_bin}:{os.environ['PATH']}", "ENVS_DIR": str(envs)}

    for layer, name, directory in (("account", "account", account), ("workspace", "dev", workspace)):
        result = subprocess.run(
            ["sh", "-c", f"umask 000; exec {RUNNER} {layer} {name} apply"],
            env=env | {"LAYER_ENV_DIR": str(directory)}, text=True, capture_output=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert _mode(directory / "terraform.tfstate") == 0o600


def test_refuses_state_symlink(tmp_path):
    target = tmp_path / "outside"
    target.write_text("{}")
    (tmp_path / "terraform.tfstate").symlink_to(target)

    try:
        tighten_state_files(tmp_path)
    except RuntimeError as exc:
        assert "symlink" in str(exc)
    else:
        raise AssertionError("state symlink was accepted")
