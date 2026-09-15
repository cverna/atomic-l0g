# atomic-l0g

Consolidate the image-based Linux ecosystem into a queryable store that AI agents
can analyse without re-fetching the world on every run.

`atomic-l0g` watches the projects competing with — and derived from — RHEL Image Mode
and bootc, collects their development activity, releases and blogs, normalises
everything into one schema, and exposes it through a CLI that agents can drive.

## Design idea

Separate **fetching** from **analysis**.

| | Fetching | Analysis |
|---|---|---|
| Who | `atomic-l0g` collectors | an AI agent |
| When | scheduled / on demand | on demand |
| Cost | deterministic, cheap, incremental | LLM, expensive |
| Output | normalised JSONL + SQLite | reports, digests, annotations |

## Status

Phase 0 — scaffold. The registry, data model and validation are in place; collectors
are not yet implemented. See [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Then:

```bash
al0g sources validate     # lint the registry
al0g sources list         # list watched projects
pytest
```

## Configuration

Collection needs read-only API tokens:

| Variable | Purpose |
|---|---|
| `GITHUB_TOKEN` | GitHub REST (required — unauthenticated is 60 req/hr) |
| `GITLAB_TOKEN` | GitLab REST v4 with the `read_api` scope |

Tokens are read from the environment and are never written to the store.

## Layout

```
sources/    declarative registry: projects, repos, tiers, bots, themes
src/        package code
data/       collected JSONL + cursors (committed); SQLite (derived, ignored)
reports/    generated digests
```
