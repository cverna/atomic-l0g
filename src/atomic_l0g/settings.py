"""Configuration and secret loading.

Secrets come from files in ``/run/secrets`` (the Podman/Docker convention) or
from environment variables.  Environment variables win, because that is what
a CI run wants.

The mounted secret files are dash-named -- ``/run/secrets/github-token`` --
while a Python field has to be ``github_token``.  ``pydantic-settings`` builds
both names as lookup candidates when a ``validation_alias`` is set together
with ``populate_by_name``, so the dash-named file is found *and* the
``GITHUB_TOKEN`` environment variable keeps working.

Secret values are never logged, never echoed in errors and never written to the
store.  Only the absence of a secret is ever reported.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["SecretMissing", "Secrets", "Settings", "repo_root"]

#: Directory holding mounted secret files.
SECRETS_DIR = Path(os.environ.get("ATOMIC_L0G_SECRETS_DIR", "/run/secrets"))


def repo_root() -> Path:
    """Return the repository root (``src/atomic_l0g/settings.py`` -> root)."""
    return Path(__file__).resolve().parents[2]


class SecretMissing(RuntimeError):
    """A required secret is available neither as a file nor as a variable."""

    def __init__(self, field: str, file_name: str, env_name: str) -> None:
        super().__init__(
            f"missing secret {field!r}: mount it at "
            f"{SECRETS_DIR / file_name} or set the {env_name} environment variable"
        )
        self.field = field
        self.file_name = file_name
        self.env_name = env_name


#: field name -> (secret file name in SECRETS_DIR, environment variable)
SECRET_SPECS = {
    "github_token": ("github-token", "GITHUB_TOKEN"),
    "gitlab_token": ("gitlab-token", "GITLAB_TOKEN"),
    "gitea_token": ("gitea-token", "GITEA_TOKEN"),
}


class Secrets(BaseSettings):
    """API tokens, loaded from secret files or the environment."""

    model_config = SettingsConfigDict(
        secrets_dir=SECRETS_DIR,
        case_sensitive=False,
        populate_by_name=True,
        extra="ignore",
    )

    github_token: str | None = Field(default=None, validation_alias="github-token")
    gitlab_token: str | None = Field(default=None, validation_alias="gitlab-token")
    gitea_token: str | None = Field(default=None, validation_alias="gitea-token")

    def require(self, field: str) -> str:
        """Return a token, or raise :class:`SecretMissing`.

        Raises rather than returning ``None`` so a missing token surfaces as a
        clear message instead of a confusing 403 several calls later.
        """
        value = getattr(self, field, None)
        if value:
            return value
        file_name, env_name = SECRET_SPECS[field]
        raise SecretMissing(field, file_name, env_name)


class Settings(BaseSettings):
    """Non-secret configuration.

    Every field can be overridden with an ``ATOMIC_L0G_``-prefixed environment
    variable, e.g. ``ATOMIC_L0G_DATA_DIR``.
    """

    model_config = SettingsConfigDict(
        env_prefix="ATOMIC_L0G_",
        extra="ignore",
        populate_by_name=True,
    )

    data_dir: Path = Field(default_factory=lambda: repo_root() / "data")
    default_window: str = "7d"
    default_tier: str = "watch"

    #: Where the derived index lives.  Separate from ``data_dir`` so a
    #: deployment can mount the store read-only and keep the index -- which is
    #: rebuilt on read when the store has moved on -- on a writable volume.
    index_path: Optional[Path] = None

    @property
    def normalized_dir(self) -> Path:
        return self.data_dir / "normalized"

    @property
    def cursors_dir(self) -> Path:
        return self.data_dir / "cursors"

    @property
    def annotations_dir(self) -> Path:
        return self.data_dir / "annotations"

    @property
    def db_path(self) -> Path:
        return self.index_path or (self.data_dir / "atomic-l0g.db")
