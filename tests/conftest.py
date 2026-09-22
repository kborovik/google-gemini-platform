import os
from pathlib import Path
from typing import Any

import pytest

from talos.constants import REQUIRED_ENV
from talos.env import fill_missing, load_terraform_output
from talos.env import repo_root as find_repo_root
from tests.helpers import load_facts, load_golden_queries, load_manifest

_GCP_ENV_NAMES = (
    *REQUIRED_ENV,
    "GCS_URI",
    "GOOGLE_APPLICATION_CREDENTIALS",
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--no-terraform",
        action="store_true",
        default=False,
        help="Do not fill missing env vars from `infra/outputs.json`.",
    )


@pytest.fixture(scope="session", autouse=True)
def fill_terraform_env(request: pytest.FixtureRequest) -> None:
    if request.config.getoption("--no-terraform"):
        return
    fill_missing(os.environ, load_terraform_output())


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return find_repo_root()


@pytest.fixture(scope="session")
def facts(repo_root: Path) -> dict[str, Any]:
    return load_facts(repo_root)


@pytest.fixture(scope="session")
def manifest(repo_root: Path) -> dict[str, Any]:
    return load_manifest(repo_root)


@pytest.fixture(scope="session")
def golden_queries(repo_root: Path) -> list[dict[str, Any]]:
    return load_golden_queries(repo_root)


@pytest.fixture
def clean_gcp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _GCP_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def clean_azure_env(clean_gcp_env: None) -> None:
    """Name kept so ported document-generation tests clear cloud env."""
    return clean_gcp_env


def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("teams"):
        pytest.skip("Google Chat is not part of v1")


@pytest.fixture(scope="session")
def live_env() -> dict[str, str]:
    from talos.env import missing_required, resolve_env

    env = resolve_env(use_terraform=True)
    missing = missing_required(env)
    if missing:
        pytest.skip("live Google Cloud env missing: " + ", ".join(missing))
    return env
