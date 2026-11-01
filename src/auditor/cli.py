"""Command-line entry point: `auditor <command>`."""

import typer
from pydantic import ValidationError

from auditor import __version__
from auditor.config import get_settings

app = typer.Typer(help="GitHub repository inventory and auditor.", no_args_is_help=True)


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


if __name__ == "__main__":
    app()
