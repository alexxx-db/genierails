"""State-changing make targets refuse -i before recipe guards can be skipped."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_check_target import DEV_ENV, _build, _env, _snapshot

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "shared"
REFUSAL = "-i/-k is not supported for this target because guards would be skipped"
PROD_ENV = '''governance_mode = "deterministic"
promote_from = "dev"
catalog_map = { "paycat" = "ppay" }
deployer_principal_id = "d0000000-0000-0000-0000-00000000d001"
genie_spaces = [
  { genie_space_id = "s1", uc_tables = ["ppay.s.t"] },
]
'''


def _tree_snapshot(root: Path) -> dict[str, bytes | str | None]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in sorted(dirnames + filenames):
            path = Path(dirpath) / name
            out[str(path.relative_to(root))] = (
                os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None
            )
    return out


def _bare(tmp_path: Path) -> Path:
    repo = tmp_path / "bare"
    (repo / "aws").mkdir(parents=True)
    shutil.copy2(ROOT / "aws" / "Makefile", repo / "aws" / "Makefile")
    (repo / "shared").symlink_to(SHARED)
    return repo


def _promoted(tmp_path: Path) -> Path:
    repo = _build(tmp_path, DEV_ENV)
    prod = repo / "aws" / "envs" / "prod"
    prod.mkdir()
    (prod / "env.auto.tfvars").write_text(PROD_ENV)
    return repo


def _make(repo: Path, tmp_path: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["make", "--no-print-directory", "-C", str(repo / "aws"), *args],
        text=True,
        capture_output=True,
        timeout=120,
        env=_env(tmp_path, **(env or {})),
    )


RUN_MODES = (
    pytest.param(("-i",), {}, id="make-i"),
    pytest.param(("-ik", "-j4"), {}, id="make-ik-j4"),
    pytest.param((), {"MAKEFLAGS": "-i"}, id="makeflags-i"),
)


@pytest.mark.parametrize(("make_flags", "env"), RUN_MODES)
@pytest.mark.parametrize(("target", "target_args", "builder"), [
    ("generate", ("ENV=prod",), _promoted),
    ("setup", ("ENV=dev",), _bare),
    ("promote", ("SOURCE_ENV=dev", "DEST_ENV=prod", "DEST_CATALOG_MAP=paycat=ppay"), _promoted),
    ("rehearse", ("ENV=dev",), _promoted),
    ("release-plan", ("ENV=prod",), _promoted),
    ("apply", ("ENV=dev",), _promoted),
])
def test_state_changing_targets_refuse_ignore_errors_before_recipes(tmp_path, target, target_args, builder,
                                                                    make_flags, env):
    repo = builder(tmp_path)
    before = _tree_snapshot(repo)

    result = _make(repo, tmp_path, *make_flags, target, *target_args, env=env)
    output = result.stdout + result.stderr

    assert result.returncode != 0, output
    assert REFUSAL in output, output
    assert "Entering directory" not in output
    assert _tree_snapshot(repo) == before


def test_makeflags_parser_only_reads_the_short_flag_word(tmp_path):
    repo = _promoted(tmp_path)
    before = _tree_snapshot(repo)

    result = _make(repo, tmp_path, "generate", "ENV=prod", env={"MAKEFLAGS": "--include-dir=/tmp FOO=i"})
    output = result.stdout + result.stderr

    assert REFUSAL not in output
    assert "make generate ENV=prod refused" in output
    assert _tree_snapshot(repo) == before


def test_keep_going_does_not_skip_a_failed_guard_prerequisite(tmp_path):
    makefile = tmp_path / "Makefile"
    stamp = tmp_path / "stamp"
    makefile.write_text(
        ".PHONY: guarded guard\n"
        "guard:\n"
        "\t@exit 7\n"
        "guarded: guard\n"
        f"\t@touch {stamp}\n"
    )

    result = subprocess.run(["make", "-k", "-f", str(makefile), "guarded"], text=True, capture_output=True)

    assert result.returncode != 0
    assert not stamp.exists()


@pytest.mark.parametrize("target", [
    ("help",),
    ("validate", "ENV=dev"),
    ("check", "ENV=dev"),
    ("status", "ENV=dev"),
    ("next", "ENV=dev"),
    ("evidence", "ENV=dev", "OFFLINE=1"),
])
def test_read_only_targets_still_run_under_ignore_errors(tmp_path, target):
    repo = _promoted(tmp_path)
    before = _snapshot(repo / "aws" / "envs")

    result = _make(repo, tmp_path, "-i", *target)
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert REFUSAL not in output
    assert _snapshot(repo / "aws" / "envs") == before
