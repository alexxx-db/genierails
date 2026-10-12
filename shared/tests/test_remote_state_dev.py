import os
import subprocess
from pathlib import Path

import pytest

from scripts import environment_lock, state_layout

ROOT = Path(__file__).resolve().parents[2]

def remote(cloud, env):
    env.mkdir(parents=True)
    if cloud == "aws":
        env.joinpath("remote_state.hcl").write_text('backend="s3"\nbucket="b"\nregion="r"\ndynamodb_table="locks"\n')
    else:
        env.joinpath("remote_state.hcl").write_text('backend="azurerm"\nresource_group_name="r"\nstorage_account_name="s"\ncontainer_name="c"\n')

@pytest.mark.parametrize("cloud", ["aws", "azure"])
def test_ci_allows_only_dev_rehearse_with_locked_remote_state(tmp_path, cloud):
    env = tmp_path/cloud/"envs/dev"; remote(cloud, env)
    base = os.environ | {"CI":"true"}
    common = ["make", "-f", str(ROOT/cloud/"Makefile"), f"ENV_DIR={env}"]
    ok = subprocess.run(common+["rehearse", "ENV=dev"], env=base, text=True, capture_output=True)
    assert "applying targets cannot run in CI" not in ok.stdout+ok.stderr
    for target, name in (("apply","dev"),("rehearse","prod"),("apply-account","account")):
        bad=subprocess.run(common+[target,f"ENV={name}"],env=base,text=True,capture_output=True)
        assert bad.returncode and "applying targets cannot run in CI" in bad.stdout+bad.stderr

def test_state_dir_isolates_state_receipts_and_lock(tmp_path, monkeypatch):
    cloud=tmp_path/"aws"; env=cloud/"envs/dev"; env.mkdir(parents=True)
    outside=tmp_path/"outside"; monkeypatch.setenv("GENIERAILS_STATE_DIR",str(outside))
    assert state_layout.env_state(env,"dev") == outside/"aws/dev"
    ok,_,_=environment_lock.acquire(env,os.getpid(),"rehearse")
    assert ok and (outside/"aws/dev/locks/.governance.lock").is_file()
    assert not (env/"generated/.governance.lock").exists()
    environment_lock.release_lock(env,os.getpid())

def test_migration_never_overwrites(tmp_path, monkeypatch):
    env=tmp_path/"aws/envs/dev"; (env/"data_access").mkdir(parents=True)
    (env/"terraform.tfstate").write_text("workspace")
    (env/"data_access/terraform.tfstate").write_text("data")
    receipts=tmp_path/"aws/.genierails-state/dev/receipts/rehearse"; receipts.mkdir(parents=True)
    receipts.joinpath("one.json").write_text("immutable")
    monkeypatch.setenv("GENIERAILS_STATE_DIR",str(tmp_path/"state"))
    state_layout.migrate(env,"dev")
    assert (tmp_path/"state/aws/dev/layers/workspace/terraform.tfstate").read_text()=="workspace"
    assert (tmp_path/"state/aws/dev/layers/data_access/terraform.tfstate").read_text()=="data"
    assert (tmp_path/"state/aws/dev/receipts/rehearse/one.json").read_text()=="immutable"
    (env/"terraform.tfstate").write_text("new")
    with pytest.raises(RuntimeError,match="refusing to overwrite"): state_layout.migrate(env,"dev")
