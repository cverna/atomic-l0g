"""Sync orchestration: walk the registry, dispatch to collectors, flush the store.

Isolation rules live here rather than in the collectors: a broken target records
an error and the run continues, and running out of API budget stops the run
cleanly with everything collected so far already persisted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from atomic_l0g.collectors import base
from atomic_l0g.collectors.feed import collect_feed
from atomic_l0g.collectors.github import collect_repo
from atomic_l0g.http import RateLimitExhausted, github_client, plain_client
from atomic_l0g.model import utcnow
from atomic_l0g.registry import Project, Registry
from atomic_l0g.settings import SecretMissing, Secrets, Settings
from atomic_l0g.store.jsonl import JsonlStore, WriteStats

__all__ = ["FEED_TIER", "SyncReport", "TargetOutcome", "sync"]

log = logging.getLogger("atomic_l0g.sync")

#: Feeds are their own pseudo-tier.  They need no credentials and consume no
#: API budget, so they are collected first: a rate-limited run then still gets
#: all of its blog content.
FEED_TIER = "feed"

#: Providers that have a collector.  Others are reported as skipped, not failed.
_COLLECTED = frozenset({"github", "feed"})

#: Collection order, highest first, so that if the budget runs out it runs out
#: on the least important targets.
_ORDER = {"feed": 4, "core": 3, "watch": 2, "release-only": 1}


@dataclass
class Target:
    """One thing to collect: a repository or a feed."""

    distro: str
    provider: str
    key: str  # "owner/repo" for repositories, a feed label for feeds
    tier: str
    url: str | None = None


@dataclass
class TargetOutcome:
    """What happened to one target."""

    distro: str
    provider: str
    target: str
    tier: str
    stats: WriteStats = field(default_factory=WriteStats)
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    skipped: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.skipped is None

    @property
    def label(self) -> str:
        """Display name.  Feed labels are namespaced by project, repos are not."""
        if self.provider == "feed":
            return f"{self.distro}:{self.target}"
        return self.target


@dataclass
class SyncReport:
    observed: str
    window: str
    since: str
    outcomes: list[TargetOutcome] = field(default_factory=list)
    stopped_early: str | None = None
    missing_secrets: list[str] = field(default_factory=list)

    @property
    def stats(self) -> WriteStats:
        total = WriteStats()
        for outcome in self.outcomes:
            total.merge(outcome.stats)
        return total

    @property
    def errors(self) -> list[TargetOutcome]:
        return [o for o in self.outcomes if o.error]

    @property
    def skipped(self) -> list[TargetOutcome]:
        return [o for o in self.outcomes if o.skipped]


def _select(registry: Registry, distros: list[str] | None) -> list[Project]:
    if not distros:
        return [project for _name, project in sorted(registry.projects.items())]

    unknown = [name for name in distros if name not in registry.projects]
    if unknown:
        available = ", ".join(sorted(registry.projects))
        raise ValueError(f"unknown distro(s): {', '.join(unknown)}; available: {available}")

    return [registry.projects[name] for name in distros]


def _targets(
    registry: Registry,
    projects: list[Project],
    tier: str | None,
    default_tier: str,
) -> list[Target]:
    targets: list[Target] = []

    for project in projects:
        for provider, repo in project.repositories():
            repo_tier = registry.repo_tiers.get(repo, default_tier)
            if tier is not None and repo_tier != tier:
                continue
            targets.append(Target(project.name, provider, repo, repo_tier))

        # Feeds belong to no tier of their own, so they are collected whenever
        # the project is selected and no specific repository tier was asked for.
        if tier is None or tier == FEED_TIER:
            for label, url in project.feeds.items():
                targets.append(Target(project.name, "feed", label, FEED_TIER, url))

    targets.sort(key=lambda target: -_ORDER.get(target.tier, 0))
    return targets


def sync(
    registry: Registry,
    settings: Settings,
    *,
    distros: list[str] | None = None,
    tier: str | None = None,
    window: str | None = None,
    with_comments: bool = True,
) -> SyncReport:
    """Collect every selected target into the store."""
    window = window or settings.default_window
    observed = utcnow()
    since = base.window_start(window)

    report = SyncReport(observed=observed, window=window, since=since)
    store = JsonlStore(settings.normalized_dir)
    secrets = Secrets()

    targets = _targets(
        registry, _select(registry, distros), tier, settings.default_tier
    )
    if not targets:
        return report

    github: httpx.Client | None = None
    feeds: httpx.Client | None = None

    try:
        if any(target.provider == "github" for target in targets):
            try:
                github = github_client(secrets)
            except SecretMissing as missing:
                report.missing_secrets.append(str(missing))
                log.error("%s", missing)

        if any(target.provider == "feed" for target in targets):
            feeds = plain_client()

        for target in targets:
            outcome = TargetOutcome(
                distro=target.distro,
                provider=target.provider,
                target=target.key,
                tier=target.tier,
            )
            report.outcomes.append(outcome)

            if target.provider not in _COLLECTED:
                outcome.skipped = f"no {target.provider} collector yet"
                continue

            cursor_id = (
                f"{target.distro}:{target.key}"
                if target.provider == "feed"
                else target.key
            )
            cursor = base.Cursor.for_target(
                settings.cursors_dir, target.provider, cursor_id
            )

            try:
                if target.provider == "feed":
                    if feeds is None:
                        outcome.skipped = "no feed client"
                        continue
                    result = collect_feed(
                        feeds,
                        registry.projects[target.distro],
                        target.key,
                        target.url or "",
                        cursor,
                        observed,
                    )
                else:
                    if github is None:
                        outcome.skipped = "no credentials"
                        continue
                    result = collect_repo(
                        github,
                        registry,
                        target.key,
                        target.distro,
                        target.tier,
                        cursor.get("last_sync") or since,
                        cursor,
                        observed,
                        with_comments=with_comments,
                    )
            except RateLimitExhausted as exhausted:
                report.stopped_early = str(exhausted)
                log.warning("%s", exhausted)
                break
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                outcome.error = f"{type(exc).__name__}: {exc}"
                log.warning("%s: %s", target.key, outcome.error)
                continue

            outcome.stats = store.write(
                [*result.items, *result.comments, *result.releases], observed
            )
            outcome.notes = result.notes

            # The cursor advances only after the records are safely written.
            cursor.set("last_sync", observed)
            cursor.set("last_window", window)
            for key, value in result.cursor.items():
                cursor.set(key, value)
            cursor.save()
    finally:
        if github is not None:
            github.close()
        if feeds is not None:
            feeds.close()

    return report
