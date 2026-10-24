"""A hand-edited prod catalog_map that maps a catalog to itself (finding S2), no promote.

promote-to refuses an identity map, but someone can still write one into
envs/prod/env.auto.tfvars by hand. Every make path that can use it (rehearse,
release-plan, release / release MANIFEST=, recorded and direct imports) refuses
before taking a lock, writing, deriving or applying anything, unless prod sets
allow_identity_catalog_map = true. Same harness as test_release_gate_make.py.
"""

from __future__ import annotations

import json

import pytest

from scripts import release_gate as rg
from tests.test_release_gate import PROD_ENV, Repo
from tests.test_release_gate_make import CLASSIFICATION_IMPORT, Make, _no_lock

IDENTITY_PROD = PROD_ENV.replace('{ "dev_cat" = "prod_cat" }', '{ "prod_cat" = "prod_cat" }')
OPT_IN = "# Separate metastores only.\nallow_identity_catalog_map = true\n"
REFUSAL = "catalog_map in envs/prod/env.auto.tfvars maps a catalog to itself (prod_cat=prod_cat)"
PROD_FILE = "aws/envs/prod/env.auto.tfvars"

assert IDENTITY_PROD != PROD_ENV


@pytest.fixture
def repo(tmp_path, monkeypatch):
    return Repo(tmp_path, monkeypatch)


@pytest.fixture
def make(tmp_path, repo):
    return Make(tmp_path, repo)


def _refused(result) -> None:
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert REFUSAL in out
    assert "set allow_identity_catalog_map = true in envs/prod/env.auto.tfvars" in out


def _no_manifest(repo: Repo) -> bool:
    store = rg.manifests_dir(repo.cloud, "prod")
    return not list(store.glob("*/manifest.json")) and not list(store.glob(".pending-*")) \
        and not list(store.glob("*/imports.jsonl"))


def _planned(make: Make) -> str:
    assert make("rehearse", "dev").returncode == 0
    result = make("release-plan", "prod")
    assert result.returncode == 0, result.stdout + result.stderr
    make.reset()
    return result.stdout.split("make release ENV=prod MANIFEST=")[-1].strip()


def test_release_plan_refuses_a_hand_edited_identity_map_before_anything(make, repo):
    assert make("rehearse", "dev").returncode == 0  # rehearsed with the distinct map
    repo.commit({PROD_FILE: IDENTITY_PROD})
    make.imports.write_text(json.dumps(CLASSIFICATION_IMPORT) + "\n")  # an import it would record
    make.reset()

    result = make("release-plan", "prod")

    _refused(result)
    assert make.calls() == [] and _no_lock(repo, "prod") and _no_manifest(repo)


def test_release_with_a_manifest_refuses_a_map_hand_edited_after_planning(make, repo):
    """release MANIFEST= and the internal _release-manifest both re-check before applying."""
    make.imports.write_text(json.dumps(CLASSIFICATION_IMPORT) + "\n")
    manifest = _planned(make)
    repo.commit({PROD_FILE: IDENTITY_PROD})
    importer = make._recorder("importer")
    (repo.prod / "data_access").mkdir(exist_ok=True)

    for target in ("release", "_release-manifest"):
        make.reset()
        result = make(target, "prod", f"MANIFEST={manifest}", f"IMPORT_EXISTING_SCRIPT={importer}")
        _refused(result)
        assert make.calls() == [], target
        assert _no_lock(repo, "prod")
        assert not rg.release_receipts(repo.cloud, "prod")
        assert not rg.phase_records(repo.cloud, "prod", repo.head())


def test_rehearse_refuses_when_its_promotion_target_has_an_identity_map(make, repo):
    commit = repo.commit({PROD_FILE: IDENTITY_PROD})

    result = make("rehearse", "dev")

    _refused(result)
    assert make.calls() == [] and _no_lock(repo, "dev")
    assert not rg.passing_receipt(repo.cloud, "dev", commit)


@pytest.mark.parametrize("target", ["import", "plan", "apply", "apply-governance", "maintain"])
def test_import_plan_apply_and_maintain_refuse_a_hand_edited_identity_map(make, repo, target):
    repo.commit({PROD_FILE: IDENTITY_PROD})

    result = make(target, "prod")

    _refused(result)
    assert "Import Existing Resources" not in result.stdout
    assert make.calls() == []


def test_the_opt_in_keeps_rehearse_release_plan_release_and_import_working(make, repo):
    repo.commit({PROD_FILE: IDENTITY_PROD + OPT_IN})
    make.imports.write_text(json.dumps(CLASSIFICATION_IMPORT) + "\n")

    manifest = _planned(make)  # rehearse dev, then release-plan prod
    importer = make._recorder("importer")
    (repo.prod / "data_access").mkdir(exist_ok=True)
    result = make("_release-manifest", "prod", f"MANIFEST={manifest}", f"IMPORT_EXISTING_SCRIPT={importer}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert [c for c in make.calls() if c[0] == "importer"]
    make.reset()
    result = make("import", "prod")
    assert REFUSAL not in result.stdout + result.stderr
    assert "_bootstrap" in make.names()


def test_a_string_opt_in_or_one_in_the_source_env_does_not_count(make, repo):
    dev = (repo.dev / "env.auto.tfvars").read_text()
    repo.commit({PROD_FILE: IDENTITY_PROD + 'allow_identity_catalog_map = "true"\n',
                 "aws/envs/dev/env.auto.tfvars": dev + "allow_identity_catalog_map = true\n"})

    for target, env in (("rehearse", "dev"), ("release-plan", "prod")):
        make.reset()
        _refused(make(target, env))
        assert make.calls() == []
