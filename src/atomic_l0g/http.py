"""Authenticated HTTP clients and pagination helpers.

Token rules, enforced here so no collector has to remember them:

* tokens travel in request headers, never in a URL query string;
* tokens are never logged or included in error output;
* a missing token raises :class:`~atomic_l0g.settings.SecretMissing` naming both
  the expected file and the environment variable.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import httpx

from atomic_l0g import __version__
from atomic_l0g.settings import Secrets

__all__ = [
    "GITHUB_API",
    "GITLAB_API",
    "RateLimitExhausted",
    "github_client",
    "gitlab_client",
    "paginate",
]

log = logging.getLogger("atomic_l0g.http")

GITHUB_API = "https://api.github.com"
GITLAB_API = "https://gitlab.com/api/v4"

USER_AGENT = f"atomic-l0g/{__version__}"

#: GitHub returns at most 100 records per page.
PAGE_SIZE = 100


class RateLimitExhausted(RuntimeError):
    """The API budget ran out.  Progress is kept; the next run resumes."""


def _client(base_url: str, headers: dict[str, str]) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        headers={"User-Agent": USER_AGENT, **headers},
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
    )


def github_client(secrets: Secrets) -> httpx.Client:
    """An authenticated GitHub REST client."""
    return _client(
        GITHUB_API,
        {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {secrets.require('github_token')}",
        },
    )


def gitlab_client(secrets: Secrets) -> httpx.Client:
    """An authenticated GitLab v4 REST client."""
    return _client(
        GITLAB_API,
        {
            "Accept": "application/json",
            "PRIVATE-TOKEN": secrets.require("gitlab_token"),
        },
    )


def _check_budget(response: httpx.Response, context: str) -> None:
    remaining = response.headers.get("x-ratelimit-remaining")
    if remaining is None:
        return
    try:
        left = int(remaining)
    except ValueError:
        return
    if left <= 0:
        reset = response.headers.get("x-ratelimit-reset", "unknown")
        raise RateLimitExhausted(
            f"API rate limit exhausted while {context} (resets at {reset}). "
            "Progress has been saved; re-run to resume."
        )


def paginate(
    client: httpx.Client,
    url: str,
    params: dict[str, Any] | None = None,
    *,
    stop: Any = None,
    max_pages: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield records across pages, stopping early when ``stop`` says so.

    ``stop`` is an optional predicate: when it returns True for a record, no
    further pages are requested.  This is what keeps an incremental sync of an
    endpoint sorted by recency cheap.  ``max_pages`` is a hard ceiling so one
    enormous repository cannot eat the whole API budget.
    """
    query = dict(params or {})
    query.setdefault("per_page", PAGE_SIZE)
    page = 1

    while True:
        query["page"] = page
        response = client.get(url, params=query)
        if response.status_code == 404:
            log.warning("not found: %s", url)
            return
        response.raise_for_status()
        _check_budget(response, f"GET {url}")

        batch = response.json()
        if not isinstance(batch, list) or not batch:
            return

        for record in batch:
            if stop is not None and stop(record):
                return
            yield record

        if len(batch) < int(query["per_page"]):
            return
        if max_pages is not None and page >= max_pages:
            return
        page += 1
