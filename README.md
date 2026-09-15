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

Phases 0 and 1 are done: registry, data model, GitHub collector, append-only
JSONL store, SQLite index and the `sync` / `list` / `show` / `fetch` commands.
Feed, structured and GitLab collectors are next. See
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Then:

```bash
al0g sources validate              # lint the registry
al0g sources list                  # list watched projects
al0g sync --distro flatcar         # collect
al0g list --since 7d               # read from the index
al0g fetch <id> --diff             # reach past the store on demand
```

Verify end to end by running `al0g sync --distro flatcar` twice: the second run
must report `new 0`.

## Configuration

Collection needs read-only API tokens, read from `/run/secrets` or the
environment:

| File | Environment variable | Purpose |
|---|---|---|
| `/run/secrets/github-token` | `GITHUB_TOKEN` | GitHub REST (required — unauthenticated is 60 req/hr) |
| `/run/secrets/gitlab-token` | `GITLAB_TOKEN` | GitLab REST v4, `read_api` scope |
| `/run/secrets/gitea-token` | `GITEA_TOKEN` | Gitea / forge.fedoraproject.org |

Secret values are never logged and never written to the store. Non-secret
settings are overridable with an `ATOMIC_L0G_` prefix, e.g. `ATOMIC_L0G_DATA_DIR`.

## Layout

```
sources/    declarative registry: projects, repos, tiers, bots, themes
src/        package code
data/       collected JSONL + cursors (committed); SQLite (derived, ignored)
reports/    generated digests
```
