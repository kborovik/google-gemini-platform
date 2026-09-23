from __future__ import annotations

from pathlib import Path

import click
from click.shell_completion import get_completion_class

from docgen import __version__
from docgen.constants import (
    APPLICATION_TYPES,
    DEFAULT_APPLICATION_CONTAINER,
    DEFAULT_APPLICATION_OUTPUT_RELATIVE,
    DEFAULT_CHAT_MODEL,
    DEFAULT_CONTAINER,
    DEFAULT_FACTS_RELATIVE,
    DEFAULT_OUTPUT_RELATIVE,
    DEFAULT_TEMPLATES_RELATIVE,
)
from docgen.env import repo_root, require_env, resolve_env
from docgen.errors import TalosError

_COMPLETION_SHELLS = ("bash", "zsh", "fish", "powershell")


def _emit_completion(
    ctx: click.Context, _param: click.Parameter, value: str | None
) -> None:
    if not value or ctx.resilient_parsing:
        return
    complete_cls = get_completion_class(value.lower())
    if complete_cls is None:
        raise SystemExit(2)
    script = complete_cls(
        cli=ctx.command,
        ctx_args={},
        prog_name="docgen",
        complete_var="_DOCGEN_COMPLETE",
    ).source()
    click.echo(script, nl=not script.endswith("\n"))
    ctx.exit()


def _run(action: object) -> None:
    try:
        action()  # type: ignore[operator]
    except TalosError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(exc.exit_code) from exc


@click.group()
@click.option(
    "--completion",
    type=click.Choice(_COMPLETION_SHELLS, case_sensitive=False),
    callback=_emit_completion,
    expose_value=False,
    is_eager=True,
    help="Print a completion script for SHELL and exit.",
)
@click.version_option(version=__version__, prog_name="docgen")
def cli() -> None:
    """Generate credit-policy documents, upload them, and index Agent Search."""


@cli.group(invoke_without_command=True)
@click.pass_context
def generate(ctx: click.Context) -> None:
    """Render synthetic credit policies or client applications."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        raise SystemExit(2)


@generate.command("policy")
@click.option("--out", type=click.Path(path_type=Path, file_okay=False), default=None)
@click.option(
    "--facts",
    "facts_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
)
@click.option(
    "--templates",
    "templates_dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
)
@click.option("--container", default=DEFAULT_CONTAINER, show_default=True)
@click.option("--bucket", default=None, help="GCS bucket. Default $GCS_BUCKET.")
@click.option("--local-only", is_flag=True, help="Write local files only.")
@click.option(
    "--gcs-only", is_flag=True, help="Upload objects only; do not write local files."
)
@click.option(
    "--dry-run", is_flag=True, help="Render and log paths without writing or uploading."
)
@click.option("--force", is_flag=True, help="Upload even if content_sha256 matches.")
@click.option(
    "--fail-if-missing-gcs", is_flag=True, help="Exit 2 if GCS is not configured."
)
@click.option(
    "--no-terraform",
    is_flag=True,
    help="Do not fill missing env vars from `infra/outputs.json`.",
)
def generate_policy(
    out: Path | None,
    facts_path: Path | None,
    templates_dir: Path | None,
    container: str,
    bucket: str | None,
    local_only: bool,
    gcs_only: bool,
    dry_run: bool,
    force: bool,
    fail_if_missing_gcs: bool,
    no_terraform: bool,
) -> None:
    """Render the twelve credit-policy Markdown files from facts.yaml."""
    from docgen.generate import GenerateConfig, run_generate

    def action() -> None:
        root = repo_root()
        config = GenerateConfig(
            out=out or (root / DEFAULT_OUTPUT_RELATIVE),
            facts_path=facts_path or (root / DEFAULT_FACTS_RELATIVE),
            templates_dir=templates_dir or (root / DEFAULT_TEMPLATES_RELATIVE),
            container=container,
            bucket=bucket or "",
            local_only=local_only,
            gcs_only=gcs_only,
            dry_run=dry_run,
            force=force,
            fail_if_missing_gcs=fail_if_missing_gcs,
            use_terraform=not no_terraform,
        )
        run_generate(config, echo=click.echo)

    _run(action)


@generate.command("application")
@click.option("--type", "application_type", type=click.Choice(APPLICATION_TYPES))
@click.option(
    "--all", "all_types", is_flag=True, help="Generate one filing of each outcome."
)
@click.option("--count", type=int, default=None, help="Filings per selected outcome.")
@click.option(
    "--application-id", default=None, help="With --force, regenerate this serial."
)
@click.option("--out", type=click.Path(path_type=Path, file_okay=False), default=None)
@click.option(
    "--facts",
    "facts_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
)
@click.option("--container", default=DEFAULT_APPLICATION_CONTAINER, show_default=True)
@click.option("--bucket", default=None, help="GCS bucket. Default $GCS_BUCKET.")
@click.option(
    "--project", default=None, help="GCP project. Default $GOOGLE_CLOUD_PROJECT."
)
@click.option(
    "--location", default=None, help="GCP region. Default $GOOGLE_CLOUD_LOCATION."
)
@click.option("--model", default=DEFAULT_CHAT_MODEL, show_default=True)
@click.option("--local-only", is_flag=True, help="Write local files only.")
@click.option(
    "--dry-run", is_flag=True, help="Print serials. No LLM call and no upload."
)
@click.option(
    "--force",
    is_flag=True,
    help="Regenerate one named filing. Requires --application-id.",
)
@click.option(
    "--no-terraform",
    is_flag=True,
    help="Do not fill missing env vars from `infra/outputs.json`.",
)
def generate_application_cmd(
    application_type: str | None,
    all_types: bool,
    count: int | None,
    application_id: str | None,
    out: Path | None,
    facts_path: Path | None,
    container: str,
    bucket: str | None,
    project: str | None,
    location: str | None,
    model: str,
    local_only: bool,
    dry_run: bool,
    force: bool,
    no_terraform: bool,
) -> None:
    """Generate opaque synthetic customer filings via Gemini. Hidden operator constraint selects intended_outcome. Does not PUT a knowledge source."""
    from docgen.application import ApplicationGenerateConfig, run_generate_application

    def action() -> None:
        if force and count is not None:
            raise TalosError("--force cannot be combined with --count", exit_code=1)
        if force and not (application_id or "").strip():
            raise TalosError("--force requires --application-id", exit_code=1)
        if (application_id or "").strip() and not force:
            raise TalosError("--application-id requires --force", exit_code=1)
        if force and all_types:
            raise TalosError("--force cannot be combined with --all", exit_code=1)
        if count is not None and not application_type and not all_types:
            raise TalosError("--count requires --type or --all", exit_code=1)
        if count is not None and count < 1:
            raise TalosError("--count must be >= 1", exit_code=1)
        if not force and bool(application_type) == bool(all_types):
            raise TalosError("exactly one of --type or --all is required", exit_code=1)
        root = repo_root()
        env = resolve_env(use_terraform=not no_terraform)
        if all_types:
            types = APPLICATION_TYPES
        elif application_type:
            types = (application_type,)
        else:
            types = ()
        config = ApplicationGenerateConfig(
            out=out or (root / DEFAULT_APPLICATION_OUTPUT_RELATIVE),
            facts_path=facts_path or (root / DEFAULT_FACTS_RELATIVE),
            types=types,
            force=force,
            local_only=local_only,
            dry_run=dry_run,
            use_terraform=not no_terraform,
            bucket=bucket or "",
            container=container,
            project=project or env.get("GOOGLE_CLOUD_PROJECT") or "",
            location=location or env.get("GOOGLE_CLOUD_LOCATION") or "",
            model=model,
            count=1 if count is None else count,
            application_id=(application_id or "").strip() or None,
        )
        run_generate_application(config, echo=click.echo)

    _run(action)


@cli.command()
@click.option(
    "--project", default=None, help="GCP project. Default $GOOGLE_CLOUD_PROJECT."
)
@click.option(
    "--location", default=None, help="GCP region. Default $GOOGLE_CLOUD_LOCATION."
)
@click.option("--bucket", default=None, help="GCS bucket. Default $GCS_BUCKET.")
@click.option(
    "--force", is_flag=True, help="Re-upload objects even when content_sha256 matches."
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the upload plan without calling Google Cloud.",
)
@click.option(
    "--no-terraform",
    is_flag=True,
    help="Do not fill missing env vars from `infra/outputs.json`.",
)
def upload(
    project: str | None,
    location: str | None,
    bucket: str | None,
    force: bool,
    dry_run: bool,
    no_terraform: bool,
) -> None:
    """Hash-skip both prefixes to the bucket. Does not import the data store."""
    from docgen.deploy import DeployConfig, run_deploy

    def action() -> None:
        env = resolve_env(use_terraform=not no_terraform)
        resolved = {
            "GOOGLE_CLOUD_PROJECT": project or env.get("GOOGLE_CLOUD_PROJECT") or "",
            "GOOGLE_CLOUD_LOCATION": location or env.get("GOOGLE_CLOUD_LOCATION") or "",
            "GCS_BUCKET": bucket or env.get("GCS_BUCKET") or "",
        }
        require_env(resolved)
        config = DeployConfig(
            project=resolved["GOOGLE_CLOUD_PROJECT"],
            location=resolved["GOOGLE_CLOUD_LOCATION"],
            bucket=resolved["GCS_BUCKET"],
            dry_run=dry_run,
            force=force,
        )
        run_deploy(config, echo=click.echo)

    _run(action)


@cli.command("index")
@click.option(
    "--project", default=None, help="GCP project. Default $GOOGLE_CLOUD_PROJECT."
)
@click.option(
    "--location", default=None, help="GCP region. Default $GOOGLE_CLOUD_LOCATION."
)
@click.option("--bucket", default=None, help="GCS bucket. Default $GCS_BUCKET.")
@click.option(
    "--wait",
    is_flag=True,
    help="Poll indexed counts until they meet the floors.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the index plan. Does not call Discovery Engine.",
)
@click.option(
    "--no-terraform",
    is_flag=True,
    help="Do not fill missing env vars from `infra/outputs.json`.",
)
def index(
    project: str | None,
    location: str | None,
    bucket: str | None,
    wait: bool,
    dry_run: bool,
    no_terraform: bool,
) -> None:
    """Ensure data store kb-credit-policies and import both GCS prefixes."""
    from docgen.search_index import index_config_from_env, run_index

    def action() -> None:
        config = index_config_from_env(
            project=project,
            location=location,
            bucket=bucket,
            wait=wait,
            dry_run=dry_run,
            use_terraform=not no_terraform,
        )
        run_index(config, echo=click.echo)

    _run(action)
