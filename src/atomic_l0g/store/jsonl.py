"""Append-only JSONL store with dedup and revision tracking.

The JSONL files are the source of truth and are committed to git, which buys
free history and readable diffs.  Records are keyed by ``id``:

* unknown id            -> append, ``new``
* known id, new content -> append a revision, ``revised``
* known id, same content -> append nothing, ``unchanged``

Because a revision is a second line with the same id, readers must treat the
*last* occurrence as current.  That is what makes the file append-only while
still describing change over time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from atomic_l0g.model import record_hash, utcnow

__all__ = ["JsonlStore", "WriteStats"]


@dataclass
class WriteStats:
    """What one write pass did."""

    new: int = 0
    revised: int = 0
    unchanged: int = 0
    untriageable: int = 0

    @property
    def written(self) -> int:
        return self.new + self.revised

    def merge(self, other: WriteStats) -> None:
        self.new += other.new
        self.revised += other.revised
        self.unchanged += other.unchanged
        self.untriageable += other.untriageable


class JsonlStore:
    """Append-only, deduplicated JSONL storage rooted at a directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        index: dict[str, dict[str, Any]] = {}
        for path in sorted(self.root.glob("*.jsonl")):
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "id" in record:
                        index[record["id"]] = record  # last occurrence wins
        return index

    def __len__(self) -> int:
        return len(self._index)

    def ids(self) -> Iterable[str]:
        return self._index.keys()

    def get(self, record_id: str) -> dict[str, Any] | None:
        """Return the current record for ``id``, or ``None``."""
        return self._index.get(record_id)


    def write(self, records: Iterable[Any], observed: str) -> WriteStats:
        """Persist records, appending only what is new or changed."""
        stats = WriteStats()
        lines: list[str] = []

        for record in records:
            payload = record.to_dict()
            record_id = payload.get("id")
            if not record_id:
                continue

            digest = record_hash(payload)
            existing = self._index.get(record_id)

            if existing is not None and existing.get("content_hash") == digest:
                stats.unchanged += 1
                continue

            payload["content_hash"] = digest
            payload["observed_at"] = observed
            if existing is None:
                payload["first_seen"] = observed
                stats.new += 1
            else:
                payload["first_seen"] = existing.get("first_seen", observed)
                stats.revised += 1
            payload["last_changed"] = observed

            if payload.get("item_kind") is not None and not (
                (payload.get("title") or "").strip()
                or (payload.get("summary") or "").strip()
            ):
                stats.untriageable += 1

            lines.append(json.dumps(payload, sort_keys=True, ensure_ascii=False))
            self._index[record_id] = payload

        if lines:
            self._append(lines)
        return stats

    def _append(self, lines: list[str]) -> None:
        path = self.root / f"{utcnow()[:7]}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")

    def read_all(self, *, newest_per_id: bool = True) -> list[dict[str, Any]]:
        """Return every record, newest occurrence per id by default."""
        if newest_per_id:
            return list(self._index.values())

        records: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.jsonl")):
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
        return records
