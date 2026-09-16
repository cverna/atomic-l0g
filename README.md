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

Phases 0–3 and 5 are done: registry, data model, GitHub and feed collectors,
append-only JSONL store, SQLite index, the CLI, and a read-only MCP server over
stdio and streamable HTTP. Remaining: the structured collectors and GitLab
(Phase 2), the analytics commands (Phase 4), and the OpenShift deployment
(Phase 6). See [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

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
al0g sync --distro flatcar         # collect repos and feeds
al0g stats --since 7d              # counts by project
al0g top --since 7d                # rank by recent discussion activity
al0g releases --since 90d          # release timeline + cadence
al0g list --kind pr --state merged # what merged, by kind and state
al0g search sysext                 # full-text search
al0g comments <id> --since 7d        # stored comments: the argument behind a count
al0g fetch <id> --diff             # reach past the store on demand
```

Read commands repair the index themselves if it is missing or stale, so there
is no build step to remember.

Verify end to end by running `al0g sync --distro flatcar` twice: the second run
must report `new 0`.

## Prompts

`prompts/weekly-digest.md` drives an agent through the weekly cross-ecosystem
digest. It doubles as the specification for the interface: if a prompt needs
something the commands cannot express, that is a missing command, not a reason
to read the data files.

## MCP server

Read-only tools over the same store, so an agent never touches the CLI's output
format or the storage layout.

```bash
python -m pip install -e ".[mcp]"     # the extra; the CLI does not need it
atomic-l0g-mcp --transport stdio      # for a local client
```

Client configuration, local:

```json
{"mcpServers": {"atomic-l0g": {
  "command": "/workspace/atomic-l0g/.venv/bin/python",
  "args": ["-m", "atomic_l0g.mcp.server", "--transport", "stdio"]}}}
```

Served over HTTP, for a shared endpoint:

```bash
ATOMIC_L0G_ALLOWED_HOSTS="al0g-mcp.apps.example.com" \
  atomic-l0g-mcp --transport streamable-http --host 0.0.0.0 --port 8000
```

Exposed tools are read-only: `ecosystem_stats`, `ecosystem_top`,
`ecosystem_list`, `ecosystem_releases`, `ecosystem_search`, `ecosystem_show`,
`ecosystem_sources`, `ecosystem_comments`.

**`sync` and `fetch` deliberately have no tool.** `sync` writes to the source of
truth; `fetch` would spend whatever credentials the process holds.
`ecosystem_comments` is the counter-example that shows the rule is about
credentials rather than depth — it reads comments from the local store, so it
needs no network and is safe alongside everything else.

Result sets are capped (200, or 100 for search) and requests are clamped rather
than rejected, so an agent never has to retry to get an answer.

Host and Origin validation is on by default. The allow-list matching is exact
or `host:*` — there is no wildcard — so a public deployment must set
`ATOMIC_L0G_ALLOWED_HOSTS` to its Route hostname; unset means localhost only.
`/healthz` is available for liveness and readiness probes.

The derived index can be kept off the store's volume, which is what lets a
deployment mount the store read-only:

```bash
ATOMIC_L0G_DATA_DIR=/store ATOMIC_L0G_INDEX_PATH=/tmp/atomic-l0g.db atomic-l0g-mcp ...
```

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
data/       collected JSONL + cursors; SQLite index is derived and ignored
prompts/    task prompts for agents; also the interface specification
scripts/    daily-ingest.sh -- what the timer or CronJob runs
deploy/     systemd units and the single-VM deployment guide
```

## Deployment

One host runs a timer that collects and a service that serves MCP over HTTP,
with a deliberate split: the collecting process holds the GitHub token, the
network-facing one holds nothing. See [deploy/README.md](deploy/README.md).
