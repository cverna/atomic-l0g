"""RSS/Atom collector.

Blog posts and announcements become ordinary items with ``item_kind="blog"``,
so one query covers development activity and blog content together.

Feeds beat the GitHub API on one axis that matters here: most supply a real
``summary``, which is precisely the field an agent triages on.  Nothing is
synthesised -- title, summary and body are whatever the feed provides.

Feeds are re-read in full on every sync rather than filtered by the cursor.
They are small (tens of entries), the store deduplicates by id so a re-read
costs nothing, and a full read cannot miss an entry that was published with a
date older than the watermark.
"""

from __future__ import annotations

import calendar
import hashlib
import logging
import re
import time
from html import unescape
from typing import Any

import feedparser
import httpx

from atomic_l0g.collectors.base import Cursor, SyncResult
from atomic_l0g.http import request
from atomic_l0g.model import Item
from atomic_l0g.registry import Project

__all__ = ["collect_feed"]

log = logging.getLogger("atomic_l0g.feed")

#: Feeds are XML; some servers content-negotiate badly without a nudge.
FEED_ACCEPT = "application/atom+xml, application/rss+xml, application/xml;q=0.9, */*;q=0.8"

_TAGS = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def _plain(text: str | None) -> str | None:
    """Collapse a feed's HTML description into readable text.

    Applied to ``summary`` only.  It is the field an agent triages on, and
    roughly one feed in five embeds full markup there.  ``body`` keeps the
    original markup so nothing is lost.  This normalises what the source
    provided; it does not invent content.
    """
    if not text:
        return None
    collapsed = _WHITESPACE.sub(" ", unescape(_TAGS.sub(" ", text))).strip()
    return collapsed or None


def _entry_key(entry: Any) -> str:
    """A stable identity for one feed entry.

    ``guid`` is preferred, but it is frequently a URL.  Hashing keeps the item
    id short and free of the colons and slashes that would make it ambiguous
    against the ``provider:...:kind:number`` convention.
    """
    raw = entry.get("id") or entry.get("link") or entry.get("title") or ""
    return hashlib.sha256(str(raw).encode("utf-8")).hexdigest()[:12]


def _timestamp(entry: Any, *keys: str) -> str | None:
    """First available date among ``keys``, as ISO-8601 UTC.

    feedparser normalises dates into ``time.struct_time`` in UTC, so
    ``calendar.timegm`` (not ``time.mktime``) is the correct conversion --
    ``mktime`` would interpret them as local time.
    """
    for key in keys:
        parsed = entry.get(f"{key}_parsed")
        if parsed:
            return time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(calendar.timegm(parsed))
            )
    return None


def _body(entry: Any) -> str | None:
    content = entry.get("content") or []
    if isinstance(content, list) and content:
        value = content[0].get("value")
        if value:
            return value
    return entry.get("summary") or None


def _labels(entry: Any) -> list[str]:
    return [
        str(tag["term"])
        for tag in (entry.get("tags") or [])
        if isinstance(tag, dict) and tag.get("term")
    ]


def _item(project: str, label: str, entry: Any, observed: str) -> Item:
    published = _timestamp(entry, "published", "updated")
    return Item(
        id=f"feed:{project}:{label}:{_entry_key(entry)}",
        distro=project,
        provider="feed",
        item_kind="blog",
        title=entry.get("title") or None,
        summary=_plain(entry.get("summary")),
        body=_body(entry),
        url=entry.get("link") or None,
        author=entry.get("author") or None,
        created_at=published,
        updated_at=_timestamp(entry, "updated", "published") or published,
        observed_at=observed,
        labels=_labels(entry),
    )


def collect_feed(
    client: httpx.Client,
    project: Project,
    label: str,
    url: str,
    cursor: Cursor,
    observed: str,
) -> SyncResult:
    """Read one feed.  Raises on transport errors; the caller isolates."""
    result = SyncResult(provider="feed", target=f"{project.name}:{label}")

    if not url:
        result.notes.append("no feed URL configured")
        return result

    response = request(client, url, headers={"Accept": FEED_ACCEPT})
    response.raise_for_status()

    parsed = feedparser.parse(response.content)
    if parsed.bozo and not parsed.entries:
        # A malformed feed is worth reporting but is not a run failure.
        result.notes.append(f"feed did not parse: {parsed.get('bozo_exception')}")
        return result

    newest: str | None = cursor.get("last_published")
    for entry in parsed.entries:
        item = _item(project.name, label, entry, observed)
        result.items.append(item)
        if item.created_at and (newest is None or item.created_at > newest):
            newest = item.created_at

    # Recorded for observability even though collection is not filtered by it.
    result.cursor["last_published"] = newest
    result.cursor["feed_title"] = parsed.feed.get("title")
    result.cursor["entry_count"] = len(parsed.entries)

    log.debug("%s:%s -> %d entries", project.name, label, len(parsed.entries))
    return result
