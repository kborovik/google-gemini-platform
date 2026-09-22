from __future__ import annotations

import tomllib

import pytest
from click.testing import CliRunner

from talos.cli import cli
from talos.env import repo_root

pytestmark = pytest.mark.unit


def test_root_help_lists_generate_deploy_and_chat() -> None:
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    for name in ("generate", "deploy", "chat"):
        assert name in result.output
    assert "publish" not in result.output


def test_bare_generate_prints_help_exit_2() -> None:
    result = CliRunner().invoke(cli, ["generate"])
    assert result.exit_code == 2
    assert "policy" in result.output
    assert "application" in result.output


def test_deploy_dry_run_prints_plan(
    clean_gcp_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "lab5-gemini-dev1")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-east1")
    monkeypatch.setenv("GCS_BUCKET", "lab5-gemini-dev1-credit-docs")
    result = CliRunner().invoke(cli, ["deploy", "--dry-run", "--no-terraform"])
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert "kb-credit-policies" in result.output


def test_deploy_missing_env_exits_2(clean_gcp_env: None) -> None:
    result = CliRunner().invoke(cli, ["deploy", "--no-terraform"])
    assert result.exit_code == 2
    assert "GOOGLE_CLOUD_PROJECT" in result.output


def test_chat_missing_project_exits_2(clean_gcp_env: None) -> None:
    result = CliRunner().invoke(cli, ["chat", "--no-terraform", "What is max LTV?"])
    assert result.exit_code == 2
    assert "GOOGLE_CLOUD_PROJECT" in result.output


def test_version_option() -> None:
    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "talos" in result.output


def test_completion_emits_click_source() -> None:
    result = CliRunner().invoke(cli, ["--completion", "fish"])
    assert result.exit_code == 0
    assert "complete" in result.output.lower() or "_TALOS_COMPLETE" in result.output


def test_completion_unknown_shell_is_usage_error() -> None:
    result = CliRunner().invoke(cli, ["--completion", "tcsh"])
    assert result.exit_code != 0


def test_pyproject_talos_cli_contract() -> None:
    data = tomllib.loads((repo_root() / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["scripts"]["talos"] == "talos.cli:cli"
    assert data["project"]["requires-python"] == ">=3.14"


def test_makefile_test_runs_pytest_and_check_calls_test() -> None:
    import re

    makefile = (repo_root() / "Makefile").read_text(encoding="utf-8")
    assert "talos test" not in makefile
    test_match = re.search(r"^test:[^\n]*\n((?:[ \t].*\n)*)", makefile, re.M)
    assert test_match is not None
    assert "$(UV) run pytest" in test_match.group(1)
    check_match = re.search(r"^check:[^\n]*\n((?:[ \t].*\n)*)", makefile, re.M)
    assert check_match is not None
    check = check_match.group(1)
    assert "ruff format --check" in check
    assert "ruff check" in check
    assert "$(MAKE) test" in check


def test_src_has_no_pep_723_scripts() -> None:
    for path in (repo_root() / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "# /// script" not in text


def test_gha_unit_workflow_calls_pytest() -> None:
    text = (repo_root() / ".github/workflows/test.yml").read_text(encoding="utf-8")
    assert "uv run pytest" in text
