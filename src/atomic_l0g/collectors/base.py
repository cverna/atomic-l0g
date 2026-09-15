"""Collector contract and shared incremental-sync plumbing.

Collector rules:

1. No LLM.
2. Idempotent -- running twice produces zero new rows.
3. Resumable -- the cursor advances only after a successful flush.
4. Failures are isolated per source; one broken repo never aborts a run.
5. Every record carries ``observed_at``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from atomic_l0g.model import Comment, Item, Release

__all__ = [
    "Cursor",
    "SyncResult",
    "compile_bots",
    "is_bot",
    "parse_window",
    "window_start",
]

_WINDOW_RE = re.compile(r"^\s*(\d+)\s*([hdw])\s*$", re.IGNORECASE)
_UNITS = {"h": "hours", "d": "days", "w": "weeks"}


def parse_window(window: str) -> timedelta:
    """Parse a window such as ``24h``, ``7d`` or ``4w``."""
    match = _WINDOW_RE.match(window)
    if not match:
        raise ValueError(f"unrecognised window {window!r}; expected e.g. 24h, 7d, 4w")
    amount, unit = int(match.group(1)), match.group(2).lower()
    return timedelta(**{_UNITS[unit]: amount})


def window_start(window: str) -> str:
    """Return the ISO-8601 instant ``window`` ago."""
    return (
        (datetime.now(timezone.utc) - parse_window(window))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def compile_bots(patterns: list[str]) -> list[re.Pattern[str]]:
    """Compile bot patterns once per run."""
    return [re.compile(pattern, re.IGNORECASE) for pattern in patterns]


def is_bot(login: str | None, patterns: list[re.Pattern[str]]) -> bool:
    """Whether ``login`` matches any bot pattern."""
    if not login:
        return False
    return any(pattern.search(login) for pattern in patterns)


@dataclass
class SyncResult:
    """Everything one collector produced for one target."""

    provider: str
    target: str
    items: list[Item] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    releases: list[Release] = field(default_factory=list)
    cursor: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.items) + len(self.comments) + len(self.releases)

    @property
    def untriageable(self) -> list[Item]:
        """Items with neither title nor summary -- reported, never fatal."""
        return [item for item in self.items if not item.has_triage_text]

    def extend(self, other: SyncResult) -> None:
        self.items.extend(other.items)
        self.comments.extend(other.comments)
        self.releases.extend(other.releases)
        self.notes.extend(other.notes)
        self.cursor.update(other.cursor)


class Cursor:
    """Per-target incremental state, persisted as JSON under ``data/cursors/``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = (
            json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        )

    @classmethod
    def for_target(cls, directory: Path, provider: str, target: str) -> Cursor:
        safe = target.replace("/", "__")
        return cls(directory / f"{provider}__{safe}.json")

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def save(self) -> None:
        """Persist.  Called only after a target flushed successfully."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
