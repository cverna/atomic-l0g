"""Core record types for atomic-l0g.

Three record kinds are collected:

* :class:`Item` -- one discrete event: an issue, PR, MR, release, blog post,
  changelog entry, CVE or commit.
* :class:`Comment` -- one comment on an item.  Stored as its own record rather
  than embedded, which turns "how many recent comments does this have?" from a
  per-item API fan-out into a local ``GROUP BY``.
* :class:`Release` -- a release normalised across heterogeneous source formats.

Every optional field is a passthrough: included when the source provides it,
omitted otherwise.  Nothing here derives, synthesises or scores content -- that
is the analysis layer's job, not the collector's.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "ITEM_KINDS",
    "STORE_MANAGED_FIELDS",
    "Comment",
    "Item",
    "Release",
    "record_hash",
    "utcnow",
]

#: Recognised item kinds.
ITEM_KINDS = frozenset(
    {
        "issue",
        "pr",
        "mr",
        "release",
        "blog",
        "patch",
        "changelog-entry",
        "cve",
        "commit",
    }
)

#: Fields the store owns.  Excluded from :func:`record_hash` so that
#: re-collecting an unchanged record can never look like a change.
STORE_MANAGED_FIELDS = frozenset(
    {"observed_at", "first_seen", "last_changed", "content_hash"}
)


def utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string ending in ``Z``."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def record_hash(record: dict[str, Any]) -> str:
    """Hash the source-owned fields of a record.

    Used for revision detection: the same item re-collected unchanged produces
    the same hash, so the store appends nothing.
    """
    payload = {
        key: value
        for key, value in record.items()
        if key not in STORE_MANAGED_FIELDS
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()



def _prune(data: dict[str, Any]) -> dict[str, Any]:
    """Drop empty values so records stay small and diffs stay readable."""
    return {key: value for key, value in data.items() if value not in (None, "", [], {})}


def _known_kwargs(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    """Filter ``data`` down to the fields ``cls`` actually declares."""
    known = {f.name for f in fields(cls)}
    return {key: value for key, value in data.items() if key in known}


@dataclass
class Item:
    """One discrete event collected from a source."""

    id: str
    distro: str
    provider: str
    item_kind: str
    title: str | None = None
    summary: str | None = None
    body: str | None = None
    url: str | None = None
    author: str | None = None
    state: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    observed_at: str | None = None
    first_seen: str | None = None
    last_changed: str | None = None
    content_hash: str | None = None
    labels: list[str] = field(default_factory=list)
    version: str | None = None
    signal: dict[str, Any] | None = None
    fetch_ref: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Item.id must not be empty")
        if self.item_kind not in ITEM_KINDS:
            raise ValueError(
                f"unknown item_kind {self.item_kind!r}; expected one of "
                f"{', '.join(sorted(ITEM_KINDS))}"
            )

    @property
    def has_triage_text(self) -> bool:
        """Whether the record carries text an agent can rank on.

        A row with neither title nor summary is untriageable; ``sync`` counts
        these and reports them rather than failing the run.
        """
        return bool((self.title or "").strip() or (self.summary or "").strip())

    def to_dict(self) -> dict[str, Any]:
        return _prune(asdict(self))


    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Item:
        return cls(**_known_kwargs(cls, data))


@dataclass
class Comment:
    """One comment on an item."""

    id: str
    parent_id: str
    provider: str
    body: str | None = None
    author: str | None = None
    created_at: str | None = None
    observed_at: str | None = None
    is_review_comment: bool = False

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Comment.id must not be empty")
        if not self.parent_id:
            raise ValueError("Comment.parent_id must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return _prune(asdict(self))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Comment:
        return cls(**_known_kwargs(cls, data))


@dataclass
class Release:
    """A release, normalised across heterogeneous source formats."""

    id: str
    distro: str
    version: str
    channel: str | None = None
    release_date: str | None = None
    components: dict[str, str] = field(default_factory=dict)
    notes: str | None = None
    url: str | None = None
    observed_at: str | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Release.id must not be empty")
        if not self.version:
            raise ValueError("Release.version must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return _prune(asdict(self))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Release:
        return cls(**_known_kwargs(cls, data))
