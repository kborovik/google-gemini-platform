from __future__ import annotations

import sys
from pathlib import Path

import click
from click.shell_completion import get_completion_class

from talos import __version__
from talos.constants import (
    APPLICATION_TYPES,
    DEFAULT_APPLICATION_CONTAINER,
    DEFAULT_APPLICATION_OUTPUT_RELATIVE,
    DEFAULT_CHAT_MODEL,
    DEFAULT_CONTAINER,
    DEFAULT_CORPUS,
    DEFAULT_FACTS_RELATIVE,
    DEFAULT_INSTRUCTIONS_RELATIVE,
    DEFAULT_OUTPUT_RELATIVE,
    DEFAULT_TEMPLATES_RELATIVE,
)
from talos.env import repo_root, require_env, resolve_env
from talos.errors import TalosError

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
        prog_name="talos",
        complete_var="_TALOS_COMPLETE",
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
@click.version_option(version=__version__, prog_name="talos")
def cli() -> None:
    """Generate, deploy, and chat with the credit-policy agent on Gemini Enterprise Agent Platform."""


@cli.group(invoke_without_command=True)
@click.pass_context
def generate(ctx: click.Context) -> None:
    """Render synthetic credit policies or client applications. Does not deploy."""
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
    from talos.generate import GenerateConfig, run_generate

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
    from talos.application import ApplicationGenerateConfig, run_generate_application

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
    "--corpus",
    default=DEFAULT_CORPUS,
    show_default=True,
    help="RAG corpus display name.",
)
@click.option(
    "--wait",
    is_flag=True,
    help="Poll the import until policy and application files are indexed.",
)
@click.option(
    "--skip-import",
    is_flag=True,
    help="Upload objects but do not import into the RAG corpus.",
)
@click.option(
    "--force", is_flag=True, help="Re-upload objects even when content_sha256 matches."
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the deploy plan without calling Google Cloud.",
)
@click.option(
    "--no-terraform",
    is_flag=True,
    help="Do not fill missing env vars from `infra/outputs.json`.",
)
def deploy(
    project: str | None,
    location: str | None,
    bucket: str | None,
    corpus: str,
    wait: bool,
    skip_import: bool,
    force: bool,
    dry_run: bool,
    no_terraform: bool,
) -> None:
    """Upload both corpora to Cloud Storage and import them into one RAG corpus."""
    from talos.deploy import DeployConfig, run_deploy

    def action() -> None:
        env = resolve_env(use_terraform=not no_terraform)
        resolved = {
            "GOOGLE_CLOUD_PROJECT": project or env.get("GOOGLE_CLOUD_PROJECT") or "",
            "GOOGLE_CLOUD_LOCATION": location or env.get("GOOGLE_CLOUD_LOCATION") or "",
            "GCS_BUCKET": bucket or env.get("GCS_BUCKET") or "",
        }
        if not dry_run:
            require_env(resolved)
        elif not all(resolved.values()):
            require_env(resolved)
        config = DeployConfig(
            project=resolved["GOOGLE_CLOUD_PROJECT"],
            location=resolved["GOOGLE_CLOUD_LOCATION"],
            bucket=resolved["GCS_BUCKET"],
            corpus_display_name=corpus,
            wait=wait,
            skip_import=skip_import,
            dry_run=dry_run,
            force=force,
        )
        run_deploy(config, echo=click.echo)

    _run(action)


@cli.command()
@click.option(
    "--project", default=None, help="GCP project. Default $GOOGLE_CLOUD_PROJECT."
)
@click.option(
    "--location", default=None, help="GCP region. Default $GOOGLE_CLOUD_LOCATION."
)
@click.option(
    "--corpus",
    default=DEFAULT_CORPUS,
    show_default=True,
    help="RAG corpus display name.",
)
@click.option("--model", default=DEFAULT_CHAT_MODEL, show_default=True)
@click.option(
    "--instructions", "instructions_path", type=click.Path(path_type=Path), default=None
)
@click.option(
    "--dry-run", is_flag=True, help="Print the chat plan without calling Gemini."
)
@click.option(
    "--no-terraform",
    is_flag=True,
    help="Do not fill missing env vars from `infra/outputs.json`.",
)
@click.argument("question", nargs=-1)
def chat(
    project: str | None,
    location: str | None,
    corpus: str,
    model: str,
    instructions_path: Path | None,
    dry_run: bool,
    no_terraform: bool,
    question: tuple[str, ...],
) -> None:
    """Ask the credit-policy agent. One-shot arguments, stdin, or a TTY REPL."""
    from talos.chat import ChatConfig, load_instructions, run_chat
    from talos.deploy import VertexRagOps
    from talos.rest import RequestsRest

    def action() -> None:
        env = resolve_env(use_terraform=not no_terraform)
        resolved_project = project or env.get("GOOGLE_CLOUD_PROJECT") or ""
        resolved_location = location or env.get("GOOGLE_CLOUD_LOCATION") or ""
        if not resolved_project or not resolved_location:
            missing = [
                name
                for name, value in (
                    ("GOOGLE_CLOUD_PROJECT", resolved_project),
                    ("GOOGLE_CLOUD_LOCATION", resolved_location),
                )
                if not value
            ]
            raise TalosError(
                "Google Cloud environment is not configured (missing "
                + ", ".join(missing)
                + "). Set the variables or run `gmake infra-create`.",
                exit_code=2,
            )
        text = " ".join(question).strip()
        interactive = False
        if not text:
            if sys.stdin.isatty():
                interactive = True
            else:
                text = sys.stdin.read().strip()
        corpus_name = corpus
        if not dry_run and "/ragCorpora/" not in corpus:
            found = VertexRagOps(
                RequestsRest(), resolved_project, resolved_location
            ).find_corpus(corpus)
            if not found:
                raise TalosError(
                    f"RAG corpus {corpus!r} was not found. Run `uv run talos deploy`.",
                    exit_code=2,
                )
            corpus_name = found
        instructions = ""
        if instructions_path is not None:
            instructions = instructions_path.read_text(encoding="utf-8")
        elif not dry_run:
            instructions = load_instructions(DEFAULT_INSTRUCTIONS_RELATIVE)
        config = ChatConfig(
            project=resolved_project,
            location=resolved_location,
            corpus_name=corpus_name,
            model=model,
            instructions=instructions,
            dry_run=dry_run,
        )
        run_chat(config, text or None, interactive=interactive, echo=click.echo)

    _run(action)
