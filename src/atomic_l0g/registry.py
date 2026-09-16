"""Loading and validation of the declarative source registry.

The registry is the single place that decides what atomic-l0g watches.  Adding
a project, a repository, a blog feed or a tier assignment is a YAML edit; no
Python changes are required.

:func:`validate_registry` is deliberately strict, so mistakes surface before a
collection run rather than as a silent gap in the store.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from atomic_l0g.model import ITEM_KINDS

__all__ = [
    "PROVIDERS",
    "REPO_TIERS",
    "Feed",
    "Project",
    "Registry",
    "ReleaseEndpoint",
    "default_sources_dir",
    "load_registry",
    "validate_registry",
]

#: Providers that have a collector implementation.
PROVIDERS = frozenset({"github", "gitlab"})

#: Cost-control tiers.
#:
#: ``core``         releases, issues, PRs and comments
#: ``watch``        releases plus issue/PR metadata
#: ``release-only`` releases and tags only
REPO_TIERS = frozenset({"core", "watch", "release-only"})


@dataclass
class ReleaseEndpoint:
    """A structured release source, e.g. Flatcar's ``releases.json``."""

    type: str
    url: str


@dataclass
class Feed:
    """An RSS/Atom source.

    ``kind`` is the ``item_kind`` its entries become.  Most feeds are blog
    posts, but a mailing-list archive is not:  lore.kernel.org serves kernel
    patches as Atom, and filing those under ``blog`` would put them in every
    ``--kind blog`` query.
    """

    url: str
    kind: str = "blog"


@dataclass
class Project:
    """One upstream project in the registry."""

    name: str
    vendor: str | None = None
    lineage: list[str] = field(default_factory=list)
    update_mechanism: str | None = None
    repos: dict[str, list[str]] = field(default_factory=dict)
    feeds: dict[str, Feed] = field(default_factory=dict)
    release_endpoints: list[ReleaseEndpoint] = field(default_factory=list)

    def repositories(self) -> list[tuple[str, str]]:
        """Return ``(provider, "owner/name")`` for every declared repository."""
        return [
            (provider, repo)
            for provider, repos in self.repos.items()
            for repo in repos
        ]


@dataclass
class Registry:
    """Everything atomic-l0g watches, loaded from ``sources/``."""

    root: Path
    projects: dict[str, Project]
    repo_tiers: dict[str, str]
    repo_project: dict[str, str]
    bots: dict[str, list[str]]
    themes: dict[str, list[str]]
    security_labels: list[str]


def default_sources_dir() -> Path:
    """Resolve the ``sources/`` directory.

    Honours ``ATOMIC_L0G_SOURCES``, then falls back to the copy shipped in the
    repository (``<repo root>/sources``).
    """
    override = os.environ.get("ATOMIC_L0G_SOURCES")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "sources"


def _read_yaml(path: Path) -> Any:
    if not path.is_file():
        return None
    with path.open("rb") as handle:
        return yaml.safe_load(handle)


def _parse_feed(value: Any) -> Feed:
    """Accept either ``label: url`` or ``label: {url: ..., kind: ...}``.

    The bare-string form is kept so existing registries do not need touching.
    """
    if isinstance(value, dict):
        return Feed(
            url=str(value.get("url") or ""),
            kind=str(value.get("kind") or "blog"),
        )
    return Feed(url=str(value or ""))


def _load_tiers(path: Path) -> dict[str, str]:
    raw = _read_yaml(path) or {}
    tiers = raw.get("tiers") or {}
    return {
        str(repo): str(tier)
        for tier, repos in tiers.items()
        for repo in (repos or [])
    }


def load_registry(sources: Path | None = None) -> Registry:
    """Load the registry from ``sources`` (or the bundled default).

    Raises:
        FileNotFoundError: if no ``distros.yaml`` is present.
        ValueError: if ``distros.yaml`` is not a mapping.
    """
    root = (
        Path(sources).expanduser().resolve()
        if sources is not None
        else default_sources_dir()
    )

    distros_path = root / "distros.yaml"
    if not distros_path.is_file():
        raise FileNotFoundError(f"no distros.yaml found in {root}")

    raw_distros = _read_yaml(distros_path) or {}
    if not isinstance(raw_distros, dict):
        raise ValueError(f"{distros_path} must contain a mapping of projects")

    projects: dict[str, Project] = {}
    for name, body in raw_distros.items():
        body = body or {}
        projects[str(name)] = Project(
            name=str(name),
            vendor=body.get("vendor"),
            lineage=list(body.get("lineage") or []),
            update_mechanism=body.get("update_mechanism"),
            repos={
                str(provider): list(repos or [])
                for provider, repos in (body.get("repos") or {}).items()
            },
            feeds={
                str(label): _parse_feed(value)
                for label, value in (body.get("feeds") or {}).items()
            },
            release_endpoints=[
                ReleaseEndpoint(type=str(item.get("type", "")), url=str(item.get("url", "")))
                for item in (body.get("release_endpoints") or [])
            ],
        )

    raw_bots = _read_yaml(root / "bots.yaml") or {}
    bots = (
        {str(provider): list(patterns or []) for provider, patterns in raw_bots.items()}
        if isinstance(raw_bots, dict)
        else {}
    )

    raw_themes = _read_yaml(root / "themes.yaml") or {}
    themes = {
        str(name): list(patterns or [])
        for name, patterns in (raw_themes.get("themes") or {}).items()
    }

    repo_project: dict[str, str] = {}
    for project in projects.values():
        for _provider, repo in project.repositories():
            repo_project.setdefault(repo, project.name)

    return Registry(
        root=root,
        projects=projects,
        repo_tiers=_load_tiers(root / "repos.yaml"),
        repo_project=repo_project,
        bots=bots,
        themes=themes,
        security_labels=list(raw_themes.get("security_labels") or []),
    )


def validate_registry(registry: Registry) -> list[str]:
    """Return a list of problems with the registry.  Empty means valid."""
    problems: list[str] = []

    if not registry.projects:
        problems.append("registry declares no projects")

    declared: dict[str, str] = {}
    for name, project in sorted(registry.projects.items()):
        if not (project.repos or project.feeds or project.release_endpoints):
            problems.append(f"{name}: declares no repos, feeds or release endpoints")

        for provider, repos in project.repos.items():
            if provider not in PROVIDERS:
                problems.append(
                    f"{name}: unknown provider {provider!r} "
                    f"(expected one of {', '.join(sorted(PROVIDERS))})"
                )
            for repo in repos:
                if "/" not in repo:
                    problems.append(f"{name}: repository {repo!r} is not 'owner/name'")
                elif repo in declared:
                    problems.append(
                        f"{name}: repository {repo!r} already declared by {declared[repo]}"
                    )
                else:
                    declared[repo] = name

                if repo not in registry.repo_tiers:
                    problems.append(f"{name}: repository {repo!r} has no tier in repos.yaml")

        for label, feed in project.feeds.items():
            if not feed.url.startswith(("http://", "https://")):
                problems.append(f"{name}: feed {label!r} is not an http(s) URL")
            if feed.kind not in ITEM_KINDS:
                problems.append(
                    f"{name}: feed {label!r} has unknown kind {feed.kind!r} "
                    f"(expected one of {', '.join(sorted(ITEM_KINDS))})"
                )

        for endpoint in project.release_endpoints:
            if not endpoint.type:
                problems.append(f"{name}: release endpoint is missing a type")
            if not endpoint.url.startswith(("http://", "https://")):
                problems.append(
                    f"{name}: release endpoint {endpoint.type!r} is not an http(s) URL"
                )

    for repo, tier in sorted(registry.repo_tiers.items()):
        if tier not in REPO_TIERS:
            problems.append(
                f"repos.yaml: {repo!r} has unknown tier {tier!r} "
                f"(expected one of {', '.join(sorted(REPO_TIERS))})"
            )
        if repo not in declared:
            problems.append(f"repos.yaml: {repo!r} is not declared by any project")

    return problems
