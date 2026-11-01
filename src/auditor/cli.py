"""Command-line entry point: `auditor <command>`."""

import typer
from pydantic import ValidationError

from auditor import __version__
from auditor.config import get_db_settings, get_settings
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
    typer.secho("Configuration OK", fg=typer.colors.GREEN)


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
