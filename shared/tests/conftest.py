"""Shared pytest fixtures for the unit test suite.

No Databricks connection, LLM call, or Terraform is required.
"""
import os
import re
import shutil
import socket
import subprocess
import tempfile
from types import SimpleNamespace

import pytest
import hcl2
from pathlib import Path

try:
    import databricks.sdk as _databricks_sdk
except ImportError:  # pragma: no cover - the verify tests require the SDK.
    _databricks_sdk = None
    _REAL_WORKSPACE_CLIENT = None
else:
    _REAL_WORKSPACE_CLIENT = _databricks_sdk.WorkspaceClient


_VERIFY_NO_NETWORK_MODULES = {
    "test_verify_effective_access.py",
    "test_verify_key_autopick.py",
    "test_verify_key_pairing.py",
    "test_verify_mask_fixed_points.py",
}


class NoNetworkIOError(BaseException):
    """Raised when a verify unit test tries to open a real network socket."""


class NoNetworkWorkspaceClient:
    """Databricks SDK stand-in for verify unit tests that use fake warehouses."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.warehouses = SimpleNamespace(list=lambda: [])
        self.service_principals = SimpleNamespace(patch=lambda *_args, **_kwargs: None)

    def __getattr__(self, name):
        raise AssertionError(
            f"unexpected Databricks WorkspaceClient.{name} use in a no-network unit test"
        )


class NoNetworkAccountClient:
    """Databricks account SDK stand-in for verify unit tests."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def __getattr__(self, name):
        raise AssertionError(
            f"unexpected Databricks AccountClient.{name} use in a no-network unit test"
        )


def _is_verify_no_network_test(item) -> bool:
    return Path(str(item.fspath)).name in _VERIFY_NO_NETWORK_MODULES


@pytest.fixture
def real_databricks_workspace_client():
    if _REAL_WORKSPACE_CLIENT is None:
        pytest.skip("databricks-sdk is not installed")
    return _REAL_WORKSPACE_CLIENT


@pytest.fixture
def no_network_io_error():
    return NoNetworkIOError


@pytest.fixture(autouse=True)
def _verify_tests_cannot_use_network(request, monkeypatch):
    """Keep verify unit tests on fake clients and fail fast on real sockets."""
    if not _is_verify_no_network_test(request.node):
        return

    def blocked_network(*_args, **_kwargs):
        raise NoNetworkIOError("real network I/O is blocked in verify unit tests")

    monkeypatch.setattr(socket, "create_connection", blocked_network)
    monkeypatch.setattr(socket, "getaddrinfo", blocked_network)
    monkeypatch.setattr(socket.socket, "connect", blocked_network)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked_network)
    try:
        import urllib3.util.connection as urllib3_connection
    except ImportError:
        pass
    else:
        monkeypatch.setattr(urllib3_connection, "create_connection", blocked_network)

    if _databricks_sdk is not None:
        monkeypatch.setattr(_databricks_sdk, "WorkspaceClient", NoNetworkWorkspaceClient)
        monkeypatch.setattr(_databricks_sdk, "AccountClient", NoNetworkAccountClient)


def _gnu_make_version(binary: str) -> tuple[int, ...] | None:
    try:
        out = subprocess.run([binary, "--version"], text=True, capture_output=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"GNU Make (\d+)\.(\d+)", out)
    return tuple(int(part) for part in m.groups()) if m else None


def _find_gnu_make() -> str | None:
    """`make` if it is GNU Make 4+, else `gmake` (Homebrew on macOS), else None.

    Apple's make is GNU Make 3.81, which rejects options such as -Oline.
    """
    for binary in ("make", "gmake"):
        path = shutil.which(binary)
        if path and (_gnu_make_version(path) or (0,)) >= (4,):
            return path
    return None


GNU_MAKE = _find_gnu_make()


@pytest.fixture(autouse=True)
def _unit_tests_are_not_ci_apply_jobs(monkeypatch):
    """Keep GitHub's ambient CI marker from changing make-driven unit tests.

    Guard tests opt back in explicitly for the subprocess they are checking.
    """
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GENIERAILS_ALLOW_CI_APPLY", raising=False)


def pytest_configure(config):
    config.addinivalue_line("markers", "gnu_make: needs GNU Make 4+ options (e.g. -Oline) from `make`")
    # Tests and recipes call plain `make`; when that is Apple's 3.81 but gmake
    # is installed, put a `make` -> gmake shim first on PATH for the session.
    if GNU_MAKE and Path(GNU_MAKE).name != "make":
        shim = Path(tempfile.mkdtemp(prefix="genierails-gnu-make-"))
        (shim / "make").symlink_to(GNU_MAKE)
        os.environ["PATH"] = f"{shim}{os.pathsep}{os.environ.get('PATH', '')}"
        config.add_cleanup(lambda: shutil.rmtree(shim, ignore_errors=True))


def pytest_runtest_setup(item):
    if item.get_closest_marker("gnu_make") and GNU_MAKE is None:
        message = "needs GNU Make 4+ (`make --version`); on macOS: brew install make, which provides gmake"
        # CI must never pass because the make-driven tests quietly didn't run.
        if os.environ.get("REQUIRE_TERRAFORM_TESTS") == "1":
            pytest.fail(f"REQUIRE_TERRAFORM_TESTS=1 but this test {message}")
        pytest.skip(message)


@pytest.fixture
def tmp_tfvars(tmp_path):
    """Return a factory that writes content to a temp .tfvars file."""
    def _write(content: str) -> Path:
        p = tmp_path / "abac.auto.tfvars"
        p.write_text(content)
        return p
    return _write


@pytest.fixture
def tmp_sql(tmp_path):
    """Return a factory that writes content to a temp .sql file."""
    def _write(content: str) -> Path:
        p = tmp_path / "masking_functions.sql"
        p.write_text(content)
        return p
    return _write


def assert_valid_hcl(path: Path) -> dict:
    """Parse path with hcl2 and return the config dict.

    Raises AssertionError with a clear message if the file is invalid HCL.
    """
    try:
        return hcl2.loads(path.read_text())
    except Exception as exc:
        raise AssertionError(
            f"File is not valid HCL after autofix:\n"
            f"  file: {path}\n"
            f"  error: {exc}\n\n"
            f"Content:\n{path.read_text()}"
        ) from exc


def pytest_collection_modifyitems(config, items):
    """With REQUIRE_TERRAFORM_TESTS=1 (CI), a missing terraform fails instead of skipping.

    The coverage-gate enforcement tests skip without terraform; CI installs it,
    and must never pass because they quietly didn't run.
    """
    if os.environ.get("REQUIRE_TERRAFORM_TESTS") == "1" and shutil.which("terraform") is None:
        raise pytest.UsageError("REQUIRE_TERRAFORM_TESTS=1 but terraform is not on PATH")
