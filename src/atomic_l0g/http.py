"""Authenticated HTTP clients and pagination helpers.

Token rules, enforced here so no collector has to remember them:

* tokens travel in request headers, never in a URL query string;
* tokens are never logged or included in error output;
* a missing token raises :class:`~atomic_l0g.settings.SecretMissing` naming both
  the expected file and the environment variable.
"""

from __future__ import annotations

import logging
import time
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
    "plain_client",
    "request",
]

log = logging.getLogger("atomic_l0g.http")

GITHUB_API = "https://api.github.com"
GITLAB_API = "https://gitlab.com/api/v4"

USER_AGENT = f"atomic-l0g/{__version__}"

#: GitHub returns at most 100 records per page.
PAGE_SIZE = 100

#: Statuses worth retrying.  GitHub returns 502/503/504 under load, and
#: timeouts on a busy repository's releases endpoint are not rare.  Without a
#: retry, one transient blip loses a whole repository for the run.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

MAX_ATTEMPTS = 3


class RateLimitExhausted(RuntimeError):
    """The API budget ran out.  Progress is kept; the next run resumes."""


def request(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    attempts: int = MAX_ATTEMPTS,
) -> httpx.Response:
    """GET with exponential backoff on transient failures.

    Returns the last response rather than raising, so the caller decides
    whether the status is fatal.
    """
    delay = 1.0
    response: httpx.Response | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = client.get(url, params=params, headers=headers)
        except httpx.TransportError:
            if attempt >= attempts:
                raise
            log.warning("transport error on %s; retrying in %.0fs", url, delay)
            time.sleep(delay)
            delay *= 2
            continue

        if response.status_code not in RETRY_STATUS:
            return response
        if attempt >= attempts:
            return response

        retry_after = response.headers.get("retry-after", "")
        wait = float(retry_after) if retry_after.isdigit() else delay
        log.warning(
            "HTTP %s from %s; retrying in %.0fs (attempt %d/%d)",
            response.status_code,
            url,
            wait,
            attempt,
            attempts,
        )
        time.sleep(wait)
        delay *= 2

    assert response is not None
    return response


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


def plain_client() -> httpx.Client:
    """An unauthenticated client, for public feeds."""
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
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
    per_page: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield records across pages, stopping early when ``stop`` says so.

    ``stop`` is an optional predicate: when it returns True for a record, no
    further pages are requested.  This is what keeps an incremental sync of an
    endpoint sorted by recency cheap.  ``max_pages`` is a hard ceiling so one
    enormous repository cannot eat the whole API budget.  ``per_page`` lets a
    caller shrink the page for heavy payloads -- see the releases case in the
    GitHub collector.
    """
    query = dict(params or {})
    if per_page is not None:
        query["per_page"] = per_page
    query.setdefault("per_page", PAGE_SIZE)
    page = 1

    while True:
        query["page"] = page
        response = request(client, url, params=query)
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
