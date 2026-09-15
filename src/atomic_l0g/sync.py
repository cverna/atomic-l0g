"""Sync orchestration: walk the registry, dispatch to collectors, flush the store.

Isolation rules live here rather than in the collectors: a broken repository
records an error and the run continues, and running out of API budget stops the
run cleanly with everything collected so far already persisted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from atomic_l0g.collectors import base
from atomic_l0g.collectors.github import TIER_ORDER, collect_repo
from atomic_l0g.http import RateLimitExhausted, github_client
from atomic_l0g.model import utcnow
from atomic_l0g.registry import Project, Registry
from atomic_l0g.settings import SecretMissing, Secrets, Settings
from atomic_l0g.store.jsonl import JsonlStore, WriteStats

__all__ = ["SyncReport", "TargetOutcome", "sync"]

log = logging.getLogger("atomic_l0g.sync")

#: Tiers that have a collector.  Others are reported as skipped, not failed.
_COLLECTED = {"github"}


@dataclass
class TargetOutcome:
    """What happened to one repository."""

    distro: str
    provider: str
    repo: str
    tier: str
    stats: WriteStats = field(default_factory=WriteStats)
    error: str | None = None
    skipped: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.skipped is None


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


def sync(
    registry: Registry,
    settings: Settings,
    *,
    distros: list[str] | None = None,
    tier: str | None = None,
    window: str | None = None,
    with_comments: bool = True,
) -> SyncReport:
    """Collect every selected repository into the store."""
    window = window or settings.default_window
    observed = utcnow()
    since = base.window_start(window)

    report = SyncReport(observed=observed, window=window, since=since)
    store = JsonlStore(settings.normalized_dir)
    secrets = Secrets()

    projects = _select(registry, distros)

    # Build the target list, most important tier first, so that if the API
    # budget runs out it runs out on the least important repositories.
    targets: list[tuple[Project, str, str, str]] = []
    for project in projects:
        for provider, repo in project.repositories():
            repo_tier = registry.repo_tiers.get(repo, settings.default_tier)
            if tier is not None and repo_tier != tier:
                continue
            targets.append((project, provider, repo, repo_tier))
    targets.sort(key=lambda t: -TIER_ORDER.get(t[3], 1))

    if not targets:
        return report

    client: httpx.Client | None = None

    try:
        if any(provider == "github" for _p, provider, _r, _t in targets):
            try:
                client = github_client(secrets)
            except SecretMissing as missing:
                report.missing_secrets.append(str(missing))
                log.error("%s", missing)

        for project, provider, repo, repo_tier in targets:
            outcome = TargetOutcome(
                distro=project.name, provider=provider, repo=repo, tier=repo_tier
            )
            report.outcomes.append(outcome)

            if provider not in _COLLECTED:
                outcome.skipped = f"no {provider} collector yet"
                continue
            if client is None:
                outcome.skipped = "no credentials"
                continue

            cursor = base.Cursor.for_target(settings.cursors_dir, provider, repo)
            effective_since = cursor.get("last_sync") or since

            try:
                result = collect_repo(
                    client,
                    registry,
                    repo,
                    project.name,
                    repo_tier,
                    effective_since,
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
                log.warning("%s: %s", repo, outcome.error)
                continue

            outcome.stats = store.write(
                [*result.items, *result.comments, *result.releases], observed
            )

            # The cursor advances only after the records are safely written.
            cursor.set("last_sync", observed)
            cursor.set("last_window", window)
            cursor.save()
    finally:
        if client is not None:
            client.close()

    return report
