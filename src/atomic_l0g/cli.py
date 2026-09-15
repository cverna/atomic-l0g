"""Command line interface for atomic-l0g.

Read commands take ``--json`` so that the eventual MCP server is a thin wrapper
over this surface rather than a rewrite.
"""

import json as jsonlib
import logging
import sqlite3
from pathlib import Path
from typing import Optional

import httpx
import typer

from atomic_l0g import __version__
from atomic_l0g.collectors import base as collector_base
from atomic_l0g.collectors.github import parse_item_id
from atomic_l0g.http import github_client, request
from atomic_l0g.registry import (
    Registry,
    default_sources_dir,
    load_registry,
    validate_registry,
)
from atomic_l0g.settings import SecretMissing, Secrets, Settings
from atomic_l0g.store import db as db_store
from atomic_l0g.store.jsonl import JsonlStore
from atomic_l0g.sync import SyncReport
from atomic_l0g.sync import sync as run_sync

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Consolidate the image-based Linux ecosystem for agent analysis.",
)
sources_app = typer.Typer(
    no_args_is_help=True, help="Inspect and validate the source registry."
)
db_app = typer.Typer(no_args_is_help=True, help="Build and query the SQLite index.")
app.add_typer(sources_app, name="sources")
app.add_typer(db_app, name="db")


@app.callback()
def main(
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show retry and isolation warnings."
    ),
) -> None:
    """Keep retry and isolation warnings visible without drowning in HTTP noise."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s", force=True)
    logging.getLogger("atomic_l0g").setLevel(logging.DEBUG if verbose else logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


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


def _connect(settings: Settings) -> sqlite3.Connection:
    if not settings.db_path.is_file():
        typer.secho(
            f"no index at {settings.db_path}; run `al0g db build` first",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    connection = sqlite3.connect(settings.db_path)
    connection.row_factory = sqlite3.Row
    return connection


@app.command()
def version() -> None:
    """Print the atomic-l0g version."""
    typer.echo(__version__)


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------


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
            f"{len(problems)} problem(s) in {registry.root}:", fg=typer.colors.RED, err=True
        )
        for problem in problems:
            typer.secho(f"  - {problem}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    repos = len(registry.repo_tiers)
    feeds = sum(len(p.feeds) for p in registry.projects.values())
    endpoints = sum(len(p.release_endpoints) for p in registry.projects.values())
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
        detail = (
            ", ".join(f"{count} {tier}" for tier, count in sorted(counts.items()))
            if counts
            else f"{len(project.feeds)} feeds"
        )
        typer.echo(f"{name:<24} {project.vendor or '-':<16} {detail}")


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


def _render_report(report: SyncReport) -> None:
    for outcome in report.outcomes:
        if outcome.error:
            typer.secho(f"  FAIL  {outcome.repo:<40} {outcome.error}", fg=typer.colors.RED)
        elif outcome.skipped:
            typer.secho(f"  skip  {outcome.repo:<40} {outcome.skipped}", fg=typer.colors.YELLOW)
        else:
            stats = outcome.stats
            marker = "  ok  " if not outcome.notes else "  part"
            colour = typer.colors.GREEN if not outcome.notes else typer.colors.YELLOW
            typer.secho(
                f"{marker}  {outcome.repo:<40} [{outcome.tier:<12}] "
                f"new {stats.new:>4}  revised {stats.revised:>3}  "
                f"unchanged {stats.unchanged:>4}",
                fg=colour,
            )
            for note in outcome.notes:
                typer.secho(f"          {note}", fg=typer.colors.YELLOW)

    stats = report.stats
    typer.echo()
    typer.secho(
        f"{len(report.outcomes)} repositories | "
        f"new {stats.new} | revised {stats.revised} | unchanged {stats.unchanged} | "
        f"untriageable {stats.untriageable}",
        bold=True,
    )

    if report.stopped_early:
        typer.secho(f"stopped early: {report.stopped_early}", fg=typer.colors.YELLOW)
    if report.missing_secrets:
        for message in report.missing_secrets:
            typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)


@app.command()
def sync(
    distro: Optional[list[str]] = typer.Option(
        None, "--distro", "-d", help="Project to sync; repeat for several. Default: all."
    ),
    tier: Optional[str] = typer.Option(
        None, "--tier", help="Only repositories at this tier (core|watch|release-only)."
    ),
    window: Optional[str] = typer.Option(
        None, "--since", help="Collection window, e.g. 24h, 7d, 4w, 90d."
    ),
    comments: bool = typer.Option(
        True, "--comments/--no-comments", help="Collect comments for core-tier repositories."
    ),
    no_index: bool = typer.Option(
        False, "--no-index", help="Skip rebuilding the SQLite index afterwards."
    ),
    sources: Optional[Path] = _sources_opt(),
) -> None:
    """Collect activity into the local store."""
    registry = _load(sources)
    settings = Settings()

    problems = validate_registry(registry)
    if problems:
        typer.secho(
            f"registry has {len(problems)} problem(s); run `al0g sources validate`",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        report = run_sync(
            registry,
            settings,
            distros=distro,
            tier=tier,
            window=window,
            with_comments=comments,
        )
    except ValueError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    _render_report(report)

    if not no_index:
        counts = db_store.build(settings.db_path, JsonlStore(settings.normalized_dir))
        typer.secho(
            f"index rebuilt: {counts['items']} items, {counts['comments']} comments, "
            f"{counts['releases']} releases",
            fg=typer.colors.BLUE,
        )

    if report.errors:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------


@db_app.command("build")
def db_build() -> None:
    """Rebuild the SQLite index from the JSONL store."""
    settings = Settings()
    store = JsonlStore(settings.normalized_dir)
    counts = db_store.build(settings.db_path, store)
    typer.secho(
        f"{settings.db_path}: {counts['items']} items, {counts['comments']} comments, "
        f"{counts['releases']} releases",
        fg=typer.colors.GREEN,
    )


# ---------------------------------------------------------------------------
# read commands
# ---------------------------------------------------------------------------


@app.command("list")
def list_items(
    distro: Optional[list[str]] = typer.Option(None, "--distro", "-d", help="Filter by project."),
    kind: Optional[list[str]] = typer.Option(None, "--kind", "-k", help="Filter by item kind."),
    window: Optional[str] = typer.Option(None, "--since", help="Only items active within, e.g. 7d."),
    limit: int = typer.Option(50, "--limit", "-n", help="Maximum rows."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List collected items, most recently updated first."""
    settings = Settings()
    connection = _connect(settings)

    where = ["item_kind IS NOT NULL"]
    params: list[object] = []

    if distro:
        where.append(f"distro IN ({','.join('?' * len(distro))})")
        params.extend(distro)
    if kind:
        where.append(f"item_kind IN ({','.join('?' * len(kind))})")
        params.extend(kind)
    if window:
        try:
            since = collector_base.window_start(window)
        except ValueError as exc:
            typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2) from exc
        where.append("COALESCE(updated_at, created_at) >= ?")
        params.append(since)

    sql = f"""
        SELECT id, distro, item_kind, title, author, state, url,
               COALESCE(updated_at, created_at) AS activity
        FROM items
        WHERE {' AND '.join(where)}
        ORDER BY activity DESC
        LIMIT ?
    """
    params.append(limit)
    rows = [dict(row) for row in connection.execute(sql, params)]

    if as_json:
        typer.echo(jsonlib.dumps(rows, indent=2, ensure_ascii=False))
        return

    if not rows:
        typer.secho("no items matched", fg=typer.colors.YELLOW)
        return

    typer.secho(f"{'ACTIVITY':<11} {'DISTRO':<22} {'KIND':<8} TITLE", bold=True)
    for row in rows:
        activity = (row["activity"] or "")[:10]
        title = (row["title"] or "(no title)")[:64]
        typer.echo(
            f"{activity:<11} {row['distro']:<22} {row['item_kind']:<8} {title}"
        )


@app.command()
def show(
    item_id: str = typer.Argument(..., help="Record id, e.g. github:flatcar/Flatcar:pr:1234"),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show one record in full."""
    settings = Settings()
    record = JsonlStore(settings.normalized_dir).get(item_id)
    if record is None:
        typer.secho(f"no record with id {item_id!r}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(jsonlib.dumps(record, indent=2, ensure_ascii=False))
        return

    for key in sorted(record):
        value = record[key]
        if key == "body" and isinstance(value, str):
            value = f"{value[:400]}{'...' if len(value) > 400 else ''}"
        typer.echo(f"{key:<16} {value}")


@app.command()
def fetch(
    item_id: str = typer.Argument(..., help="Record id to reach past the store for."),
    diff: bool = typer.Option(False, "--diff", help="Print the live pull-request diff."),
    comments: bool = typer.Option(False, "--comments", help="Print the live comments."),
    window: Optional[str] = typer.Option(None, "--since", help="Limit comments to a window."),
) -> None:
    """Reach past the store on demand.

    The store deliberately keeps no diffs: it keeps enough to find one.  This is
    that escape hatch.
    """
    settings = Settings()
    record = JsonlStore(settings.normalized_dir).get(item_id)
    if record is None:
        typer.secho(f"no record with id {item_id!r}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    provider, repo, _kind, number = parse_item_id(item_id)
    if provider != "github":
        typer.secho(f"fetch does not support provider {provider!r} yet", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    if not (diff or comments):
        typer.echo(jsonlib.dumps(record.get("fetch_ref") or {}, indent=2))
        return

    try:
        client = github_client(Secrets())
    except SecretMissing as missing:
        typer.secho(f"error: {missing}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from missing

    with client:
        if diff:
            response = request(
                client,
                f"/repos/{repo}/pulls/{number}",
                headers={"Accept": "application/vnd.github.diff"},
            )
            response.raise_for_status()
            typer.echo(response.text)

        if comments:
            params = {}
            if window:
                params["since"] = collector_base.window_start(window)
            response = request(
                client, f"/repos/{repo}/issues/{number}/comments", params=params
            )
            response.raise_for_status()
            for comment in response.json():
                author = (comment.get("user") or {}).get("login", "?")
                created = (comment.get("created_at") or "")[:10]
                typer.echo(f"--- @{author} ({created})")
                typer.echo(comment.get("body") or "")
                typer.echo()
