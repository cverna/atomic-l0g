"""GitHub collector.

Reads releases, issues, pull requests and comments for one repository, at the
detail level implied by that repository's tier:

* ``release-only``  releases
* ``watch``         releases, issues, pull requests
* ``core``          the above plus comments and review comments

Nothing here derives or scores content.  A field is present when GitHub
provides it and absent otherwise.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from atomic_l0g.collectors.base import Cursor, SyncResult, compile_bots, is_bot
from atomic_l0g.http import paginate
from atomic_l0g.model import Comment, Item, Release
from atomic_l0g.registry import Registry

__all__ = ["TIER_ORDER", "collect_repo", "parse_item_id"]

log = logging.getLogger("atomic_l0g.github")

#: Detail level per tier.  Higher includes everything below it.
TIER_ORDER = {"release-only": 0, "watch": 1, "core": 2}

#: Hard ceilings so one huge repository cannot consume the whole API budget.
#: Release pages are counted in 50s (see RELEASE_PAGE_SIZE), so 4 pages keeps
#: the previous 200-release history depth at a page size GitHub will serve.
_MAX_RELEASE_PAGES = 4
_MAX_ISSUE_PAGES = 5
_MAX_PULL_PAGES = 5
_MAX_COMMENT_PAGES = 3

#: Releases carry full release-note bodies, so 100 per page is a heavy response.
#: On a repository with large notes GitHub returns 504 for later pages at
#: ``per_page=100`` while serving ``per_page=50`` fine, so ask for less.
RELEASE_PAGE_SIZE = 50


def parse_item_id(item_id: str) -> tuple[str, str, str, str]:
    """Split ``provider:owner/repo:kind:number`` into its parts."""
    parts = item_id.split(":")
    if len(parts) < 4:
        raise ValueError(
            f"malformed item id {item_id!r}; expected provider:owner/repo:kind:number"
        )
    return parts[0], parts[1], parts[2], parts[3]


def _phase(label: str, notes: list[str], work: Any) -> None:
    """Run one collection phase; a transport failure is noted, never fatal.

    A repository is collected in phases, and a failure in one phase must not
    discard the others.  Without this, a single 504 on the releases endpoint
    loses that repository's issues, pull requests and comments too.
    """
    try:
        work()
    except (httpx.HTTPStatusError, httpx.TransportError) as exc:
        message = f"{label}: {type(exc).__name__}: {exc}"
        notes.append(message)
        log.warning("%s", message)


def _labels(raw: dict[str, Any]) -> list[str]:
    return [
        label["name"]
        for label in (raw.get("labels") or [])
        if isinstance(label, dict) and label.get("name")
    ]


def _signal(raw: dict[str, Any]) -> dict[str, Any]:
    """Passthrough of the counts the list response already carries."""
    signal: dict[str, Any] = {}
    if raw.get("comments") is not None:
        signal["comments"] = raw["comments"]
    reactions = raw.get("reactions") or {}
    if reactions.get("total_count") is not None:
        signal["reactions"] = reactions["total_count"]
    return signal


def _release(repo: str, distro: str, raw: dict[str, Any], observed: str) -> Release | None:
    tag = raw.get("tag_name") or raw.get("name")
    if not tag:
        return None
    published = raw.get("published_at") or raw.get("created_at") or ""
    return Release(
        id=f"github:{repo}:release:{tag}",
        distro=distro,
        version=str(tag),
        channel="prerelease" if raw.get("prerelease") else None,
        release_date=published[:10] or None,
        notes=raw.get("body") or None,
        url=raw.get("html_url"),
        observed_at=observed,
    )


def _issue(
    repo: str, distro: str, raw: dict[str, Any], observed: str
) -> Item:
    number = raw["number"]
    return Item(
        id=f"github:{repo}:issue:{number}",
        distro=distro,
        provider="github",
        item_kind="issue",
        title=raw.get("title"),
        body=raw.get("body"),
        url=raw.get("html_url"),
        author=(raw.get("user") or {}).get("login"),
        state=raw.get("state"),
        created_at=raw.get("created_at"),
        updated_at=raw.get("updated_at"),
        observed_at=observed,
        labels=_labels(raw),
        signal=_signal(raw),
        fetch_ref={"comments": f"repos/{repo}/issues/{number}/comments"},
    )


def _pull(
    repo: str, distro: str, raw: dict[str, Any], observed: str
) -> Item:
    number = raw["number"]
    merged = raw.get("merged_at")
    return Item(
        id=f"github:{repo}:pr:{number}",
        distro=distro,
        provider="github",
        item_kind="pr",
        title=raw.get("title"),
        body=raw.get("body"),
        url=raw.get("html_url"),
        author=(raw.get("user") or {}).get("login"),
        # GitHub reports "closed" for both merged and abandoned PRs; the
        # distinction matters, and merged_at is the fact that settles it.
        state="merged" if merged else raw.get("state"),
        created_at=raw.get("created_at"),
        updated_at=raw.get("updated_at"),
        observed_at=observed,
        labels=_labels(raw),
        signal=_signal(raw),
        fetch_ref={
            "diff": f"repos/{repo}/pulls/{number}",
            "comments": f"repos/{repo}/issues/{number}/comments",
            "review_comments": f"repos/{repo}/pulls/{number}/comments",
        },
    )


def _comment(
    repo: str, parent_id: str, raw: dict[str, Any], observed: str, *, review: bool
) -> Comment:
    return Comment(
        id=f"{parent_id}:comment:{raw.get('id')}",
        parent_id=parent_id,
        provider="github",
        body=raw.get("body"),
        author=(raw.get("user") or {}).get("login"),
        created_at=raw.get("created_at") or raw.get("submitted_at"),
        observed_at=observed,
        is_review_comment=review,
    )


def collect_repo(
    client: httpx.Client,
    registry: Registry,
    repo: str,
    distro: str,
    tier: str,
    since: str,
    cursor: Cursor,
    observed: str,
    *,
    with_comments: bool = True,
) -> SyncResult:
    """Collect one repository.  Raises on transport errors; the caller isolates."""
    result = SyncResult(provider="github", target=repo)
    patterns = compile_bots(registry.bots.get("github", []))
    level = TIER_ORDER.get(tier, 1)

    # --- releases: every tier ---
    # Bot filtering is deliberately NOT applied here.  Who published a release
    # is irrelevant -- `github-actions[bot]` publishes most of Universal Blue's
    # releases, and dropping them would silently lose the entire release stream.
    def collect_releases() -> None:
        for raw in paginate(
            client,
            f"/repos/{repo}/releases",
            per_page=RELEASE_PAGE_SIZE,
            max_pages=_MAX_RELEASE_PAGES,
        ):
            release = _release(repo, distro, raw, observed)
            if release is not None:
                result.releases.append(release)

    _phase("releases", result.notes, collect_releases)

    if level < 1:
        return result

    targets: list[tuple[Item, int, bool]] = []

    # --- issues ---
    # The issues endpoint also returns pull requests; those are filtered out
    # here and collected from /pulls instead, which carries merged_at.
    def collect_issues() -> None:
        for raw in paginate(
            client,
            f"/repos/{repo}/issues",
            {"state": "all", "sort": "updated", "direction": "desc", "since": since},
            max_pages=_MAX_ISSUE_PAGES,
        ):
            if "pull_request" in raw:
                continue
            if is_bot((raw.get("user") or {}).get("login"), patterns):
                continue
            item = _issue(repo, distro, raw, observed)
            result.items.append(item)
            targets.append((item, raw["number"], False))

    _phase("issues", result.notes, collect_issues)

    # --- pull requests ---
    # /pulls has no `since` parameter, so rely on the descending updated_at
    # ordering and stop at the first record older than the window.
    def collect_pulls() -> None:
        for raw in paginate(
            client,
            f"/repos/{repo}/pulls",
            {"state": "all", "sort": "updated", "direction": "desc"},
            stop=lambda record: (record.get("updated_at") or "") < since,
            max_pages=_MAX_PULL_PAGES,
        ):
            if is_bot((raw.get("user") or {}).get("login"), patterns):
                continue
            item = _pull(repo, distro, raw, observed)
            result.items.append(item)
            targets.append((item, raw["number"], True))

    _phase("pull requests", result.notes, collect_pulls)

    # --- comments: core tier only ---
    if level < 2 or not with_comments:
        return result

    # One failing comment fetch must not cost the rest of the repository.
    for item, number, is_pull in targets:
        def collect_issue_comments(item: Item = item, number: int = number) -> None:
            for raw in paginate(
                client,
                f"/repos/{repo}/issues/{number}/comments",
                {"since": since},
                max_pages=_MAX_COMMENT_PAGES,
            ):
                if is_bot((raw.get("user") or {}).get("login"), patterns):
                    continue
                result.comments.append(
                    _comment(repo, item.id, raw, observed, review=False)
                )

        _phase(f"comments on #{number}", result.notes, collect_issue_comments)

        if is_pull:
            def collect_review_comments(item: Item = item, number: int = number) -> None:
                for raw in paginate(
                    client,
                    f"/repos/{repo}/pulls/{number}/comments",
                    {"since": since},
                    max_pages=_MAX_COMMENT_PAGES,
                ):
                    if is_bot((raw.get("user") or {}).get("login"), patterns):
                        continue
                    result.comments.append(
                        _comment(repo, item.id, raw, observed, review=True)
                    )

            _phase(f"review comments on #{number}", result.notes, collect_review_comments)

    return result
