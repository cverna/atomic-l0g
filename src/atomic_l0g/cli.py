"""Command line interface for atomic-l0g.

Read commands gain a ``--json`` flag as collectors land (Phase 1+), so that the
eventual MCP server is a thin wrapper over this surface rather than a rewrite.
"""

from pathlib import Path
from typing import Optional

import typer

from atomic_l0g import __version__
from atomic_l0g.registry import (
    Registry,
    default_sources_dir,
    load_registry,
    validate_registry,
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Consolidate the image-based Linux ecosystem for agent analysis.",
)
sources_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect and validate the source registry.",
)
app.add_typer(sources_app, name="sources")


def _sources_opt() -> typer.Option:
    """A fresh ``--sources`` option per command, so no OptionInfo is shared."""
    return typer.Option(
        None,
        "--sources",
        envvar="ATOMIC_L0G_SOURCES",
        help="Path to the sources/ directory.",
        show_default=False,
    )


def _load(sources: Optional[Path]) -> Registry:
    try:
        return load_registry(sources)
    except (FileNotFoundError, ValueError) as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc


@app.command()
def version() -> None:
    """Print the atomic-l0g version."""
    typer.echo(__version__)


@sources_app.command("path")
def sources_path(sources: Optional[Path] = _sources_opt()) -> None:
    """Print the resolved sources directory."""
    resolved = default_sources_dir() if sources is None else sources.expanduser().resolve()
    typer.echo(resolved)


@sources_app.command("validate")
def sources_validate(sources: Optional[Path] = _sources_opt()) -> None:
    """Validate the registry, exiting non-zero if anything is wrong."""
    registry = _load(sources)
    problems = validate_registry(registry)

    if problems:
        typer.secho(
            f"{len(problems)} problem(s) in {registry.root}:",
            fg=typer.colors.RED,
            err=True,
        )
        for problem in problems:
            typer.secho(f"  - {problem}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    repos = len(registry.repo_tiers)
    feeds = sum(len(project.feeds) for project in registry.projects.values())
    endpoints = sum(len(project.release_endpoints) for project in registry.projects.values())
    typer.secho(
        f"OK  {len(registry.projects)} projects | {repos} repositories | "
        f"{feeds} feeds | {endpoints} release endpoints",
        fg=typer.colors.GREEN,
    )


@sources_app.command("list")
def sources_list(sources: Optional[Path] = _sources_opt()) -> None:
    """List the projects in the registry."""
    registry = _load(sources)

    typer.secho(f"{'PROJECT':<24} {'VENDOR':<16} TIERS", bold=True)
    for name, project in sorted(registry.projects.items()):
        counts: dict[str, int] = {}
        for _provider, repo in project.repositories():
            tier = registry.repo_tiers.get(repo, "untiered")
            counts[tier] = counts.get(tier, 0) + 1

        if counts:
            detail = ", ".join(f"{count} {tier}" for tier, count in sorted(counts.items()))
        else:
            detail = f"{len(project.feeds)} feeds"

        typer.echo(f"{name:<24} {project.vendor or '-':<16} {detail}")
