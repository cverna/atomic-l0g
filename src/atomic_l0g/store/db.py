"""Materialise the JSONL store into SQLite for fast agent queries.

SQLite is derived, never committed, and cheap to rebuild.  It exists so that
questions like "top 10 discussions this week" are a local query rather than an
API fan-out.

Records are partitioned by shape, since all three kinds share one JSONL file:

* ``item_kind`` present -> item
* ``parent_id`` present -> comment
* otherwise             -> release
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from atomic_l0g.store.jsonl import JsonlStore

__all__ = ["build", "partition"]

SCHEMA = """
DROP VIEW IF EXISTS v_item_signal;
DROP TABLE IF EXISTS items_fts;
DROP TABLE IF EXISTS items;
DROP TABLE IF EXISTS comments;
DROP TABLE IF EXISTS releases;
DROP TABLE IF EXISTS security_labels;

CREATE TABLE security_labels (
    label TEXT PRIMARY KEY
);

CREATE TABLE items (
    id           TEXT PRIMARY KEY,
    distro       TEXT,
    provider     TEXT,
    item_kind    TEXT,
    title        TEXT,
    summary      TEXT,
    url          TEXT,
    author       TEXT,
    state        TEXT,
    created_at   TEXT,
    updated_at   TEXT,
    first_seen   TEXT,
    last_changed TEXT,
    version      TEXT,
    labels       TEXT,
    data         TEXT NOT NULL
);

CREATE INDEX idx_items_distro ON items(distro);
CREATE INDEX idx_items_kind ON items(item_kind);
CREATE INDEX idx_items_updated ON items(updated_at);

CREATE TABLE comments (
    id                TEXT PRIMARY KEY,
    parent_id         TEXT,
    provider          TEXT,
    author            TEXT,
    created_at        TEXT,
    body              TEXT,
    is_review_comment INTEGER,
    data              TEXT NOT NULL
);

CREATE INDEX idx_comments_parent ON comments(parent_id);
CREATE INDEX idx_comments_created ON comments(created_at);

CREATE TABLE releases (
    id           TEXT PRIMARY KEY,
    distro       TEXT,
    version      TEXT,
    channel      TEXT,
    release_date TEXT,
    notes        TEXT,
    url          TEXT,
    data         TEXT NOT NULL
);

CREATE INDEX idx_releases_distro ON releases(distro);
CREATE INDEX idx_releases_date ON releases(release_date);

CREATE VIRTUAL TABLE items_fts USING fts5(
    id UNINDEXED,
    title,
    summary,
    body
);

-- Window-relative activity.  Deliberately a view rather than a stored column:
-- comments_recent depends on the window being asked about, so freezing it into
-- the committed records would mean rewriting history to change the window.
CREATE VIEW v_item_signal AS
SELECT
    i.id,
    i.distro,
    i.item_kind,
    i.title,
    i.url,
    i.author,
    i.state,
    i.created_at,
    i.updated_at,
    i.last_changed,
    json_extract(i.data, '$.signal.comments') AS comments,
    json_extract(i.data, '$.signal.reactions') AS reactions,
    -- Derived from the stored labels and the registry's security_labels, not
    -- stored on the record.  A stored flag could never backfill: records
    -- collected before it existed would lack it for good.
    EXISTS (
        SELECT 1
        FROM json_each(COALESCE(i.labels, '[]')) AS j
        JOIN security_labels s ON lower(j.value) = lower(s.label)
    ) AS is_security,
    (
        SELECT COUNT(*) FROM comments c
        WHERE c.parent_id = i.id
          AND c.created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7 days')
    ) AS comments_7d,
    (
        SELECT COUNT(*) FROM comments c
        WHERE c.parent_id = i.id
          AND c.created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-30 days')
    ) AS comments_30d,
    (SELECT MAX(c.created_at) FROM comments c WHERE c.parent_id = i.id) AS last_comment_at
FROM items i;
"""


def partition(
    records: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a mixed record stream into items, comments and releases."""
    items: list[dict[str, Any]] = []
    comments: list[dict[str, Any]] = []
    releases: list[dict[str, Any]] = []

    for record in records:
        if record.get("item_kind") is not None:
            items.append(record)
        elif record.get("parent_id") is not None:
            comments.append(record)
        else:
            releases.append(record)

    return items, comments, releases


def build(
    db_path: Path,
    store: JsonlStore,
    security_labels: Iterable[str] = (),
) -> dict[str, int]:
    """Rebuild the SQLite index from the JSONL store.

    ``security_labels`` comes from the registry; it is seeded into a table so
    that ``is_security`` can be derived for every record, including ones
    collected before the label list was configured.
    """
    items, comments, releases = partition(store.read_all())

    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()

    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(SCHEMA)

        connection.executemany(
            "INSERT OR IGNORE INTO security_labels (label) VALUES (?)",
            [(label,) for label in security_labels],
        )

        connection.executemany(
            """
            INSERT INTO items (id, distro, provider, item_kind, title, summary, url,
                               author, state, created_at, updated_at, first_seen,
                               last_changed, version, labels, data)
            VALUES (:id, :distro, :provider, :item_kind, :title, :summary, :url,
                    :author, :state, :created_at, :updated_at, :first_seen,
                    :last_changed, :version, :labels, :data)
            """,
            [
                {
                    "id": r["id"],
                    "distro": r.get("distro"),
                    "provider": r.get("provider"),
                    "item_kind": r.get("item_kind"),
                    "title": r.get("title"),
                    "summary": r.get("summary"),
                    "url": r.get("url"),
                    "author": r.get("author"),
                    "state": r.get("state"),
                    "created_at": r.get("created_at"),
                    "updated_at": r.get("updated_at"),
                    "first_seen": r.get("first_seen"),
                    "last_changed": r.get("last_changed"),
                    "version": r.get("version"),
                    "labels": json.dumps(r.get("labels") or []),
                    "data": json.dumps(r, ensure_ascii=False),
                }
                for r in items
            ],
        )

        connection.executemany(
            """
            INSERT INTO comments (id, parent_id, provider, author, created_at, body,
                                  is_review_comment, data)
            VALUES (:id, :parent_id, :provider, :author, :created_at, :body,
                    :is_review_comment, :data)
            """,
            [
                {
                    "id": r["id"],
                    "parent_id": r.get("parent_id"),
                    "provider": r.get("provider"),
                    "author": r.get("author"),
                    "created_at": r.get("created_at"),
                    "body": r.get("body"),
                    "is_review_comment": 1 if r.get("is_review_comment") else 0,
                    "data": json.dumps(r, ensure_ascii=False),
                }
                for r in comments
            ],
        )

        connection.executemany(
            """
            INSERT INTO releases (id, distro, version, channel, release_date, notes, url, data)
            VALUES (:id, :distro, :version, :channel, :release_date, :notes, :url, :data)
            """,
            [
                {
                    "id": r["id"],
                    "distro": r.get("distro"),
                    "version": r.get("version"),
                    "channel": r.get("channel"),
                    "release_date": r.get("release_date"),
                    "notes": r.get("notes"),
                    "url": r.get("url"),
                    "data": json.dumps(r, ensure_ascii=False),
                }
                for r in releases
            ],
        )

        connection.executemany(
            "INSERT INTO items_fts (id, title, summary, body) VALUES (?, ?, ?, ?)",
            [
                (r["id"], r.get("title") or "", r.get("summary") or "", r.get("body") or "")
                for r in items
            ],
        )

        connection.commit()
        # FTS5 segment merging keeps the index compact after a rebuild.
        connection.execute("INSERT INTO items_fts(items_fts) VALUES('optimize')")
        connection.commit()
    finally:
        connection.close()

    return {
        "items": len(items),
        "comments": len(comments),
        "releases": len(releases),
    }
