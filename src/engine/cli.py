"""Command-line entrypoints: `uv run engine <command>`."""

from __future__ import annotations

import logging

import typer
from sqlalchemy import func, select

from engine.agent import llm as llm_module
from engine.agent.planner import plan_workflow
from engine.config import settings
from engine.db import Api, Endpoint, IngestRun, Provider, init_db, reset_db, session_scope
from engine.execution import credentials as creds
from engine.execution import runner
from engine.execution import worker as worker_module
from engine.ingest.fetch import fetch_all
from engine.ingest.run import full_run, ingest_cached_specs
from engine.search import embeddings as emb
from engine.search.index import build_index, indexed_count
from engine.search.retrieve import search as run_search

app = typer.Typer(help="Autonomous API Discovery & Integration Engine", no_args_is_help=True)


def _configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG
        if verbose
        else getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx logs one line per request at INFO and a wall of frames at DEBUG.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@app.command("init-db")
def init_db_command() -> None:
    """Create tables if they do not exist."""
    _configure_logging()
    init_db()
    typer.echo(f"database ready at {settings.sqlalchemy_url}")


@app.command("reset-db")
def reset_db_command(
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Drop and recreate every table. Destroys ingested data."""
    _configure_logging()
    if not yes:
        typer.confirm("This deletes all ingested data. Continue?", abort=True)
    reset_db()
    typer.echo("database reset")


@app.command("fetch")
def fetch_command(
    limit: int = typer.Option(None, help="How many specs to download."),
    force: bool = typer.Option(False, "--force", help="Re-download cached specs."),
    per_group: int = typer.Option(1, help="Max specs per distinct API."),
    include: str = typer.Option(
        None, help="Comma-separated keys to pull in regardless of ranking."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Download OpenAPI specs from APIs.guru into the local cache."""
    _configure_logging(verbose)
    results = fetch_all(
        limit=limit,
        force=force,
        per_group=per_group,
        include=tuple(t.strip() for t in (include or "").split(",") if t.strip()),
    )
    typer.echo(f"cached {len(results)} specs in {settings.specs_dir}")


@app.command("ingest")
def ingest_command(
    limit: int = typer.Option(None, help="How many cached specs to parse."),
    fetch: bool = typer.Option(False, "--fetch", help="Download specs first."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Parse cached specs into the database."""
    _configure_logging(verbose)
    stats = full_run(limit=limit) if fetch else ingest_cached_specs(limit=limit)
    typer.echo(stats.summary())
    if stats.failures:
        typer.echo("\nfirst few skips:")
        for failure in stats.failures[:5]:
            typer.echo(f"  {failure['spec']}: {failure['error']}")


@app.command("stats")
def stats_command() -> None:
    """Show what is currently in the knowledge base."""
    _configure_logging()
    init_db()
    with session_scope() as session:
        providers = session.scalar(select(func.count()).select_from(Provider)) or 0
        apis = session.scalar(select(func.count()).select_from(Api)) or 0
        endpoints = session.scalar(select(func.count()).select_from(Endpoint)) or 0
        destructive = (
            session.scalar(
                select(func.count()).select_from(Endpoint).where(Endpoint.is_destructive.is_(True))
            )
            or 0
        )
        by_method = session.execute(
            select(Endpoint.method, func.count())
            .group_by(Endpoint.method)
            .order_by(func.count().desc())
        ).all()
        top_apis = session.execute(
            select(Api.name, func.count(Endpoint.endpoint_id))
            .join(Endpoint, Endpoint.api_id == Api.api_id)
            .group_by(Api.api_id)
            .order_by(func.count(Endpoint.endpoint_id).desc())
            .limit(10)
        ).all()
        last_run = session.scalar(select(IngestRun).order_by(IngestRun.id.desc()).limit(1))

    typer.echo(f"providers : {providers}")
    typer.echo(f"apis      : {apis}")
    typer.echo(f"endpoints : {endpoints}  ({destructive} flagged destructive)")
    typer.echo("\nby method:")
    for method, count in by_method:
        typer.echo(f"  {method:<7} {count}")
    typer.echo("\nlargest APIs:")
    for name, count in top_apis:
        typer.echo(f"  {count:>5}  {name[:60]}")
    if last_run:
        typer.echo(
            f"\nlast ingest: {last_run.specs_ok} ok / {last_run.specs_failed} skipped "
            f"at {last_run.finished_at:%Y-%m-%d %H:%M}"
        )


@app.command("index")
def index_command(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Build the BM25 full-text index from the ingested endpoints."""
    _configure_logging(verbose)
    total = build_index()
    typer.echo(f"indexed {total} endpoints")


@app.command("embed")
def embed_command(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Generate endpoint embeddings for semantic search. Requires LLM_API_KEY."""
    _configure_logging(verbose)
    try:
        total = emb.build_embeddings()
    except emb.EmbeddingError as err:
        typer.secho(f"embedding failed: {err}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from err
    typer.echo(f"embedded {total} endpoints -> {settings.embeddings_path}")


@app.command("search")
def search_command(
    query: str = typer.Argument(..., help="Natural-language capability to find."),
    mode: str = typer.Option("hybrid", help="bm25 | vector | hybrid"),
    limit: int = typer.Option(10, help="How many results to show."),
    provider: str = typer.Option(None, help="Filter by provider id."),
    method: str = typer.Option(None, help="Filter by HTTP method."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Search the endpoint knowledge base."""
    _configure_logging(verbose)
    if indexed_count() == 0:
        typer.secho("no search index — run `engine index` first", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    with session_scope() as session:
        try:
            result = run_search(
                session, query, mode=mode, limit=limit, provider=provider, method=method
            )
        except emb.EmbeddingError as err:
            typer.secho(f"{err}", fg=typer.colors.RED)
            raise typer.Exit(code=1) from err

        typer.echo(f'"{result.query}"  [{result.mode}]  {result.took_ms} ms')
        typer.echo("")
        for c in result.candidates:
            flags = " DEPRECATED" if c.is_deprecated else ""
            flags += " DESTRUCTIVE" if c.is_destructive else ""
            typer.echo(f"{c.rank:>3}. {c.score:8.4f}  {c.method:<6} {c.path[:52]}")
            typer.echo(f"      {(c.summary or '(no summary)')[:78]}")
            detail = f"      {c.provider_id} · {c.endpoint_id}{flags}"
            if c.component_ranks:
                parts = ", ".join(f"{k}#{v}" for k, v in sorted(c.component_ranks.items()))
                detail += f" · ranks: {parts}"
            typer.echo(detail)
        if not result.candidates:
            typer.echo("  no matches")


@app.command("plan")
def plan_command(
    goal: str = typer.Argument(..., help="What the automation should do, in plain English."),
    max_repairs: int = typer.Option(None, help="Validator feedback rounds before giving up."),
    show_prompt: bool = typer.Option(False, "--show-prompt", help="Print the final prompt."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Turn a natural-language goal into a validated workflow."""
    _configure_logging(verbose)
    if indexed_count() == 0:
        typer.secho("no search index — run `engine index` first", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    try:
        client = llm_module.get_client()
    except llm_module.LLMError as err:
        typer.secho(f"{err}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from err

    with session_scope() as session:
        try:
            outcome = plan_workflow(session, goal, client, max_repairs=max_repairs)
        except llm_module.LLMError as err:
            typer.secho(f"planning failed: {err}", fg=typer.colors.RED)
            raise typer.Exit(code=1) from err

    if outcome.requirement:
        typer.echo(f"\nintent   : trigger={outcome.requirement.trigger!r}")
        typer.echo(f"           actions={outcome.requirement.actions}")
    typer.echo(f"candidates: {len(outcome.candidates)} endpoints retrieved")
    typer.echo(f"attempts  : {len(outcome.attempts)}   tokens: {outcome.usage.total_tokens}")

    for attempt in outcome.attempts:
        if attempt.accepted:
            typer.secho(f"  attempt {attempt.iteration}: accepted", fg=typer.colors.GREEN)
        else:
            codes = ", ".join(i.code for i in attempt.issues)
            typer.secho(
                f"  attempt {attempt.iteration}: rejected ({codes})", fg=typer.colors.YELLOW
            )

    if outcome.ok and outcome.compiled:
        typer.secho("\nCOMPILED", fg=typer.colors.GREEN, bold=True)
        typer.echo(f"  order: {' -> '.join(outcome.compiled.execution_order)}")
        for node in outcome.compiled.nodes:
            if not node.method:
                continue
            typer.echo(f"\n  {node.id} [{node.type}] {node.method} {node.base_url}{node.path}")
            for i in node.inputs:
                src = f"{i.source_node}.{i.source_path}" if i.source_node else repr(i.literal)
                typer.echo(f"      {i.location}.{i.name} <- {src}")
    else:
        typer.secho("\nREJECTED", fg=typer.colors.RED, bold=True)
        for issue in outcome.issues:
            typer.echo(f"  [{issue.code}] node={issue.node_id} field={issue.field}")
            typer.echo(f"      {issue.message}")
            if issue.hint:
                typer.echo(f"      hint: {issue.hint}")

    if show_prompt and outcome.attempts:
        typer.echo("\n--- last prompt ---")
        typer.echo(outcome.attempts[-1].raw[:2000])


credential_app = typer.Typer(help="Manage encrypted provider credentials.")
app.add_typer(credential_app, name="credential")


@credential_app.command("add")
def credential_add(
    provider_id: str = typer.Argument(..., help="Provider id, e.g. slack_com."),
    secret: str = typer.Option(..., prompt=True, hide_input=True, help="The token."),
    scheme: str = typer.Option("bearer", help="bearer | apikey | basic | oauth2"),
    label: str = typer.Option(None, help="A note to identify this credential."),
) -> None:
    """Store a provider secret, encrypted at rest."""
    _configure_logging()
    init_db()
    with session_scope() as session:
        try:
            creds.store_credential(
                session, provider_id=provider_id, secret=secret, scheme=scheme, label=label
            )
        except creds.CredentialError as err:
            typer.secho(f"{err}", fg=typer.colors.RED)
            raise typer.Exit(code=1) from err
    # The value is never echoed back.
    typer.secho(f"stored {scheme} credential for {provider_id}", fg=typer.colors.GREEN)


@credential_app.command("list")
def credential_list() -> None:
    """Show which providers have credentials. Values are never displayed."""
    _configure_logging()
    init_db()
    from engine.db import Credential

    with session_scope() as session:
        rows = session.scalars(select(Credential)).all()
        if not rows:
            typer.echo("no credentials stored")
            return
        for row in rows:
            typer.echo(
                f"  {row.provider_id:<20} {row.scheme:<8} key v{row.key_version}  {row.label or ''}"
            )


@app.command("run")
def run_command(
    workflow_id: str = typer.Argument(..., help="The workflow to execute."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Build the request but do not send it."),
    queue: bool = typer.Option(
        False, "--queue", help="Enqueue for a worker instead of running now."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Execute a compiled workflow."""
    _configure_logging(verbose)
    init_db()
    with session_scope() as session:
        compiled = runner.load_compiled(session, workflow_id)
        if compiled is None:
            typer.secho(
                f"workflow {workflow_id} has no compiled form — validate it first",
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=1)

        execution = runner.create_execution(session, workflow_id=workflow_id, dry_run=dry_run)
        if queue:
            runner.enqueue(session, execution)
            session.commit()
            typer.echo(f"queued {execution.execution_id} — start a worker to run it")
            return

        runner.run_execution(session, execution, compiled)
        colour = typer.colors.GREEN if execution.status == "succeeded" else typer.colors.RED
        typer.secho(f"\n{execution.status.upper()}  {execution.execution_id}", fg=colour, bold=True)
        if execution.error_message:
            typer.echo(f"  [{execution.error_category}] {execution.error_message[:300]}")

        from engine.db import NodeResult

        results = session.scalars(
            select(NodeResult).where(NodeResult.execution_id == execution.execution_id)
        ).all()
        for result in results:
            typer.echo(
                f"  {result.node_id:<6} {result.status:<10} http={result.status_code} "
                f"{result.latency_ms}ms attempts={result.attempts}"
            )


@app.command("worker")
def worker_command(
    max_jobs: int = typer.Option(None, help="Stop after this many jobs."),
    poll: float = typer.Option(1.0, help="Seconds between polls when idle."),
) -> None:
    """Run the execution worker until interrupted."""
    _configure_logging()
    handled = worker_module.serve(poll_seconds=poll, max_jobs=max_jobs)
    typer.echo(f"handled {handled} job(s)")


@app.command("executions")
def executions_command(limit: int = typer.Option(10)) -> None:
    """Recent execution history."""
    _configure_logging()
    init_db()
    from engine.db import Execution

    with session_scope() as session:
        rows = session.scalars(
            select(Execution).order_by(Execution.started_at.desc()).limit(limit)
        ).all()
        if not rows:
            typer.echo("no executions yet")
            return
        for row in rows:
            flag = " [dry-run]" if row.dry_run else ""
            typer.echo(
                f"  {row.execution_id}  {row.status:<10} {row.workflow_id}{flag}"
                + (f"  ({row.error_category})" if row.error_category else "")
            )


if __name__ == "__main__":
    app()
