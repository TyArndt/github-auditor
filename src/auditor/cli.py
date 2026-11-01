"""Command-line entry point: `auditor <command>`."""

import logging

import typer
from pydantic import ValidationError

from auditor import __version__
from auditor.config import Settings, get_db_settings, get_settings
from auditor.db import migrate as db_migrate

app = typer.Typer(help="GitHub repository inventory and auditor.", no_args_is_help=True)
db_app = typer.Typer(help="Database schema commands.", no_args_is_help=True)
app.add_typer(db_app, name="db")


@app.command()
def version() -> None:
    """Print the auditor version."""
    typer.echo(__version__)


@app.command("check-config")
def check_config() -> None:
    """Validate configuration without calling GitHub or the database."""
    try:
        settings = get_settings()
    except ValidationError as exc:
        typer.secho(f"Configuration invalid:\n{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    key_source = (
        "inline secret" if settings.github_private_key else str(settings.github_private_key_path)
    )
    typer.echo(f"org:             {settings.github_org}")
    typer.echo(f"app id:          {settings.github_app_id}")
    typer.echo(f"installation id: {settings.github_installation_id}")
    typer.echo(f"private key:     {key_source}")
    typer.echo(f"api url:         {settings.github_api_url}")
    typer.echo(f"extra CA bundle: {settings.ca_bundle or '(none: public CAs only)'}")
    typer.secho("Configuration OK", fg=typer.colors.GREEN)


def _settings_or_exit() -> Settings:
    try:
        return get_settings()
    except ValidationError as exc:
        typer.secho(f"Configuration invalid:\n{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def whoami() -> None:
    """Check the GitHub App: identity, installation, permissions, org access, rate limits."""
    from auditor.whoami import whoami as run_whoami

    info = run_whoami(_settings_or_exit())
    typer.echo(f"app:              {info.app_name} ({info.app_slug})")
    typer.echo(f"installed on:     {info.account} ({info.repository_selection} repositories)")
    typer.echo(f"repos visible:    {info.repo_count}")
    typer.echo(f"org:              {info.org_login} (base permission: {info.org_base_permission})")
    for resource, limit in info.rate_limits.items():
        typer.echo(f"rate limit:       {resource} {limit['remaining']}/{limit['limit']} per hour")
    typer.echo(
        "permissions:      " + ", ".join(f"{k}={v}" for k, v in sorted(info.permissions.items()))
    )
    if info.problems:
        for problem in info.problems:
            typer.secho(f"PROBLEM: {problem}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.secho("GitHub App OK", fg=typer.colors.GREEN)


@app.command()
def sync(
    limit: int | None = typer.Option(
        None, help="Only the first N repos (trial run; nothing org-wide is soft-deleted)."
    ),
) -> None:
    """Collect from GitHub into the database (one full run)."""
    from auditor.sync import run_sync

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_sync(_settings_or_exit(), limit=limit)
    color = typer.colors.GREEN if result.status == "success" else typer.colors.YELLOW
    typer.secho(
        f"run {result.run_id}: {result.status} — {result.repos_deep_synced}/{result.repos_seen} "
        f"repos, {result.branches_seen} branches, {len(result.errors)} errors",
        fg=color,
    )
    for error in result.errors[:20]:
        typer.secho(f"  {error}", fg=typer.colors.YELLOW, err=True)
    if result.status != "success":
        raise typer.Exit(code=2)


def _database_url() -> str:
    try:
        return get_db_settings().database_url.get_secret_value()
    except ValidationError as exc:
        typer.secho("DATABASE_URL is not set (env or .env)", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@db_app.command("migrate")
def db_migrate_cmd() -> None:
    """Apply pending migrations (db/migrations) and rebuild report views (db/views)."""
    try:
        applied = db_migrate.migrate(_database_url())
    except db_migrate.MigrationError as exc:
        typer.secho(f"Migration refused: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    for name in applied:
        typer.echo(f"applied  {name}")
    typer.secho(
        f"Database up to date ({len(applied)} applied); views rebuilt.", fg=typer.colors.GREEN
    )


@db_app.command("status")
def db_status_cmd() -> None:
    """Show applied and pending migrations."""
    try:
        result = db_migrate.status(_database_url())
    except db_migrate.MigrationError as exc:
        typer.secho(f"Migration history problem: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    for name in result.applied:
        typer.echo(f"applied  {name}")
    for name in result.pending:
        typer.secho(f"pending  {name}", fg=typer.colors.YELLOW)
    if result.pending:
        raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
