# atomic-l0g — Implementation Plan

Consolidate the image-based / immutable Linux ecosystem into a queryable store that
AI agents can analyse without re-fetching the world on every run.

---

## 1. Purpose

`atomic-l0g` watches the projects competing with — and derived from — RHEL Image Mode /
bootc, collects their development activity, releases and blogs, normalises everything
into one schema, and exposes it through a CLI that agents can drive.

The central design idea: **separate fetching from analysis.**

| | Fetching | Analysis |
|---|---|---|
| Who | `atomic-l0g` collectors | an AI agent |
| When | scheduled / on demand | on demand |
| Cost | deterministic, cheap, incremental | LLM, expensive |
| Output | normalised JSONL + SQLite | reports, digests, annotations |

The existing `coreos-activity` skill does both at once. Every analysis run re-fetches
everything, hits rate limits, and produces a different answer each time. `atomic-l0g`
does the first half so the agent can do the second half off local data.

---

## 2. Locked decisions

| Decision | Value |
|---|---|
| Name | `atomic-l0g` (CLI alias `al0g`) |
| Python package | `atomic_l0g` |
| Language | Python 3.14 (venv-based; see §9) |
| Interface | CLI today; MCP server (Streamable HTTP + stdio) is the agent interface (§11 Phase 5) |
| Interaction rule | An agent talks to the CLI or MCP only — never to tables, files or the index |
| Storage | Git-committed JSONL + derived SQLite |
| Detail depth | Metadata + bodies + comments. **No diffs** — fetched on demand |
| GitHub access | REST + PAT (`/run/secrets/github-token` or `GITHUB_TOKEN`) |
| GitLab access | REST v4 + PAT (`/run/secrets/gitlab-token` or `GITLAB_TOKEN`) |
| Secrets | `pydantic-settings` reading `/run/secrets` or the environment (§9.1) |
| Collection | OpenShift CronJob, incremental, hourly (§11 Phase 6) |
| Deployment | OpenShift: collector holds the token, the exposed MCP server holds none |
| Agent surface | Read-only. `sync` is never exposed; `fetch` is local-only |
| Triage fields | `title` / `summary` / `body` — passthrough whatever the source has |
| Ranking | Frozen `signal` (comments, reactions, labels) + window-relative SQL view |
| Verification | Run it live against the real APIs. No unit tests, by choice (§12) |

---

## 3. Architecture

```
                  sources/*.yaml  (declarative registry)
                          |
                          v
   +----------------------------------------------+
   |  Collectors (no LLM, idempotent, resumable)  |
   |  github | gitlab | feed | structured         |
   +----------------------------------------------+
                          |
                          v
   +----------------------------------------------+
   |  Store                                        |
   |   data/normalized/<YYYY-MM>.jsonl  (committed)|
   |   data/cursors/*.json              (committed)|
   |              |                                |
   |              v  materialize (derived)         |
   |   atomic-l0g.db  (SQLite, gitignored)         |
   +----------------------------------------------+
                          |
                          v
   +----------------------------------------------+
   |  Query surface -- the ONLY agent interface    |
   |   CLI  `al0g ... --json`                      |
   |    +-- MCP server (Streamable HTTP, stdio)    |
   +----------------------------------------------+
```

Nobody outside this box touches the store directly. Not the agent, not the MCP
server -- both go through the CLI. Storage layout, the index and the
append-only contract stay private, so they can change without breaking a
prompt.

In deployment the two halves separate, and only one of them holds credentials:

```
   [ CronJob: collector ]              [ Deployment: MCP read server ]
    GITHUB_TOKEN from a Secret          no secrets at all
    writes JSONL + cursors     --->     mounts the store read-only
    hourly, Forbid                      index on an emptyDir
                                        Service + Route (TLS)
```

**Extensibility rule:** adding a distro is a YAML edit, never a code change. Adding a
new *kind* of source is one collector module implementing `Collector`.

---

## 4. Data model

### 4.1 Item

One record per discrete event. All optional fields are straight passthrough — present
if the source has them, omitted otherwise. No derivation, no fallback strings.

```json
{
  "id": "github:flatcar/Flatcar:pr:1234",
  "distro": "flatcar",
  "provider": "github",
  "item_kind": "pr",
  "title": "...",
  "summary": "...",
  "body": "...",
  "url": "https://github.com/flatcar/Flatcar/pull/1234",
  "author": "jlebon",
  "state": "open",
  "created_at": "2026-09-10T08:12:00Z",
  "updated_at": "2026-09-14T10:22:00Z",
  "observed_at": "2026-09-15T09:00:00Z",
  "first_seen": "2026-09-10T09:00:00Z",
  "last_changed": "2026-09-14T09:00:00Z",
  "content_hash": "sha256:...",
  "labels": ["kind/bug"],
  "version": "3815.2.4",
  "signal": { "comments": 14, "reactions": 12 },
  "fetch_ref": {
    "diff": "repos/flatcar/Flatcar/pulls/1234",
    "comments": "repos/flatcar/Flatcar/issues/1234/comments"
  }
}
```

`item_kind` ∈ `issue | pr | mr | release | blog | changelog-entry | cve | commit`.

### 4.2 Why these fields

- **`occurred_at` family vs `observed_at`** — absorbs GitHub search-index lag instead of
  being fooled by it. Backfills are safe.
- **`first_seen` vs `last_changed`** — powers "new since last digest" and "this just got hot".
- **`content_hash`** — an edited issue becomes a *revision*, not a duplicate.
- **`fetch_ref`** — the escape hatch. Store enough to retrieve the diff later; don't store
  the diff.

### 4.3 Comment

Stored as its own record, not embedded:

```json
{
  "id": "github:flatcar/Flatcar:pr:1234:comment:998877",
  "parent_id": "github:flatcar/Flatcar:pr:1234",
  "provider": "github",
  "author": "dustymabe",
  "body": "...",
  "created_at": "2026-09-13T14:02:00Z",
  "is_review_comment": true
}
```

This is the single highest-leverage modelling choice. It turns the old skill's slowest
operation — a per-item `?since=` API fan-out to count recent comments — into a local
`GROUP BY`.

### 4.4 Release

Normalised across very different source formats:

```json
{
  "id": "flatcar:release:3815.2.4",
  "distro": "flatcar",
  "version": "3815.2.4",
  "channel": "stable",
  "release_date": "2026-09-12",
  "components": { "kernel": "6.12.5", "systemd": "257", "ignition": "2.22" },
  "notes": "...",
  "url": "..."
}
```

Flatcar's `releases.json` carries `major_software` natively, so `components` is a
passthrough there. For distros without it, the field is simply absent.

### 4.5 Signal

Two tiers, deliberately split:

**Frozen into the JSONL record** — whatever the list response already gives us, no extra calls:

```json
"signal": { "comments": 14, "reactions": 12 }
```

| Source | comments | reactions | labels |
|---|---|---|---|
| GitHub | `comments` | `reactions.total_count` | `labels[].name` |
| GitLab | `user_notes_count` | `upvotes` | `labels[]` |
| Feeds / blogs | — | — | — |
| Releases | — | — | — |

**Window-relative, computed in SQLite** — never frozen, so changing the window never
re-commits the store:

```sql
v_item_signal
  item_id, comments_7d, comments_30d, last_comment_at, last_activity_at
```

No baked-in `trending_score`. Expose the raw counts; let the agent weight them. A
hardcoded formula would hide its own inputs.

### 4.6 Triage invariant

A record with neither `title` nor `summary` is untriageable. One rule, not a rules engine:

```
sync prints: synced 412 items — 0 without title/summary
```

Flag, don't fail. One bad item must not kill a 400-item run, but a source that silently
starts returning bare rows is visible immediately.

---

## 5. Source registry

`sources/distros.yaml` is the whole extensibility story.

```yaml
flatcar:
  vendor: Microsoft
  lineage: [CoreOS Container Linux]
  update_mechanism: Omaha (nebraska)
  repos:
    github: [flatcar/Flatcar, flatcar/scripts, flatcar/nebraska]
  feeds:
    blog: https://flatcar.org/blog/index.xml
  release_endpoints:
    - type: flatcar-releases-json
      url: https://flatcar.org/releases-json/releases.json
```

`sources/repos.yaml` sets a cost-control tier per repo:

| Tier | Fetches | Applies to |
|---|---|---|
| `core` | releases, issues, PRs, comments | Flatcar, ACL, Bottlerocket, Amazon Linux, Azure Linux |
| `watch` | releases, issues/PRs metadata | Talos, Kairos, openSUSE MicroOS, Elemental |
| `release-only` | releases/tags only | Universal Blue variants, peripheral tooling |

Tier is a one-line change, so a project can be promoted the moment it matters.

### 5.1 v1 scope (all verified from this container)

| Distro | GitHub | Feed / structured | Status |
|---|---|---|---|
| flatcar | `flatcar/Flatcar`, `flatcar/scripts`, `flatcar/nebraska` | `releases.json`, blog `index.xml` | ✅ richest source |
| azure-container-linux | `microsoft/azure-container-linux` | MS Learn docs | ✅ Flatcar fork |
| azure-linux | `microsoft/azurelinux`, `microsoft/AzureLinuxVulnerabilityData` | — | ✅ CVE feed bonus |
| bottlerocket | `bottlerocket-os/bottlerocket`, `twoliter`, `*-kit` | `CHANGELOG.md` raw | ✅ |
| amazon-linux | `amazonlinux/amazon-linux-2023`, **`amazon-linux-2027`** | AL2023 release notes (HTML) | ⚠️ no RSS |
| talos | `siderolabs/talos` | blog feed TBD | ⚠️ feed unconfirmed |
| kairos | `kairos-io/kairos`, `kairos-io/hadron` | blog feed TBD | ⚠️ feed unconfirmed |
| universal-blue | `ublue-os/bluefin`, `bluefin-lts`, `ucore`, `aurora`, `main` | GitHub releases | ✅ |
| opensuse-microos | `openSUSE/microos-tools`, `sysextmgr`, `combustion` | `news.opensuse.org/feed/` | ✅ |
| rancher-elemental | `rancher/elemental`, `elemental-toolkit` | — | ✅ |
| aws-blogs | — | `aws.amazon.com/blogs/containers/feed/`, `.../opensource/feed/` | ✅ |

**Baseline group (recommended, toggleable):** `coreos` org, `openshift/os`, and the
GitLab `fedora/bootc` group. Without a baseline, cross-distro comparison has nothing to
compare *to*. Remove from the registry if unwanted.

---

## 6. Collectors

Uniform interface, so adding sources never touches the store or CLI:

```python
class Collector(Protocol):
    name: str
    def supports(self, entry: RegistryEntry) -> bool: ...
    def sync(self, entry, cursor, since) -> SyncResult: ...
```

| Adapter | Covers | Incremental via |
|---|---|---|
| `github` | releases, issues, PRs, commits, tags, comments | `since=` + ETag |
| `gitlab` | group issues/MRs, notes | `updated_after` |
| `feed` | blogs, announcements | feed GUID / published date |
| `structured` | Flatcar `releases.json`, Bottlerocket CHANGELOG, AL release notes | version diff |

**Collector rules:**
1. No LLM. Ever.
2. Idempotent — running twice produces zero new rows.
3. Resumable — the cursor advances only after a successful flush.
4. Failures are isolated per source; one broken feed never aborts a run.
5. Records `observed_at` on everything.

Per-collector health is printed at the end of every `sync`, so a silent source break is
visible rather than accumulating as a quiet gap.

---

## 7. Storage

| Path | Committed | Purpose |
|---|---|---|
| `data/normalized/<YYYY-MM>.jsonl` | yes | items, comments **and releases**, interleaved |
| `data/cursors/*.json` | yes | per-repository incremental state |
| `data/annotations/*.jsonl` | yes | agent-generated summaries, keyed by item id |
| `data/atomic-l0g.db` | **no** | SQLite, rebuilt from JSONL |
| `reports/` | yes | generated digests |
| `cache/` | **no** | raw payloads, debug dumps |

One JSONL stream carries all three record kinds; they are told apart by shape
(`item_kind` → item, `parent_id` → comment, otherwise release) and split into
tables at materialisation time. Keeping one stream means one append path, one
dedup rule and one revision rule.

**Facts and interpretation stay separate.** The normalised store holds only collected
facts. An agent's better summary ("ACL is now a Flatcar fork — strategic for RHEL Image
Mode") goes to `data/annotations/`, so it can be regenerated and never contaminates the
source data.

Git commits the JSONL, which buys free history, diffs, and "what changed since last run".
SQLite is derived and never committed. Diffs are absent entirely, which is what keeps the
repo small.

---

## 8. CLI

```
al0g sources validate | list | show <project>   # registry + collection health
al0g sync [--distro X] [--since 7d] [--tier core]   # WRITE -- collector only
al0g stats    --since 7d                        # counts by project
al0g top      --since 7d [--security]           # ranked by recent discussion
al0g list     --kind pr --state merged --security
al0g releases [--distro X] [--since 90d] [--channel stable]
al0g search   "sysext" [--kind blog]            # FTS5, bm25 ranked
al0g show     <id>                              # one full record
al0g fetch    <id> --diff | --comments [--since 7d]  # escape hatch, live API
al0g db build                                   # derived index
```

Planned in Phase 4: `al0g cadence | versions | themes | lineage`.

Every read command takes `--json` and `--since`, and repairs the index itself
when the store has moved on, so there is no build step to remember.

**MCP exposes the same surface, read-only:**

| MCP tool | Backed by |
|---|---|
| `ecosystem_stats` | `al0g stats --json` |
| `ecosystem_top` | `al0g top --json` |
| `ecosystem_list` | `al0g list --json` |
| `ecosystem_releases` | `al0g releases --json` |
| `ecosystem_search` | `al0g search --json` |
| `ecosystem_show` | `al0g show --json` |
| `ecosystem_sources` | `al0g sources show --json` |
| `ecosystem_comments` | `al0g comments --json` |

`sync` and `fetch` get no MCP tool. `sync` writes to the source of truth;
`fetch` would spend the serving pod's credentials, and the serving pod is
deliberately given none. `ecosystem_comments` is the counter-example that shows
the rule is about credentials rather than about depth: it reads comments from
the local store, so it needs no network and is safe alongside everything else.

**Interaction rule:** an agent talks to the CLI or MCP and nothing else. If a
question needs a filter neither exposes, the answer is to add a command — not
to read the JSONL or open the index. That rule is what keeps the storage free
to change, and it is why the CLI surface is the thing that grows.

---

## 9. Development environment (virtualenv)

Verified working: Python 3.14.7, `venv` + `ensurepip` present, pip 26.0.1, SQLite 3.51.2
with **FTS5** and **JSON1** compiled in (so full-text search needs no extra dependency),
PyPI reachable.

```bash
git init atomic-l0g && cd atomic-l0g          # or use the existing /workspace/atomic-l0g
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

`pyproject.toml` runtime dependencies — deliberately small:

| Dep | Why |
|---|---|
| `httpx` | HTTP for all collectors |
| `feedparser` | RSS/Atom parsing |
| `typer` | CLI ergonomics |
| `pyyaml` | the declarative registry |
| `pydantic-settings` | typed config, and secret loading from files |

Stdlib covers the rest: `sqlite3`, `json`, `hashlib`, `datetime`.

**Conventions**
- `.venv/` is gitignored, never committed.
- `python -m pip` inside the venv, always.
- Nothing secret is ever committed, logged or written to the store.

### 9.1 Secrets

Tokens are read from files in `/run/secrets` (the Podman/Docker convention) or
from environment variables; environment variables win, which is what CI wants.

```
/run/secrets/github-token   ->  Secrets().github_token
/run/secrets/gitlab-token   ->  Secrets().gitlab_token
/run/secrets/gitea-token    ->  Secrets().gitea_token
```

The wrinkle: the mounted files are dash-named, while a Python field must be
`github_token`. `pydantic-settings` maps field names to file names, so a plain
field silently finds nothing. Setting a `validation_alias` alongside
`populate_by_name` makes the source emit **both** `github-token` and
`github_token` as lookup candidates — the dash-named file is found and
`GITHUB_TOKEN` keeps working:

```python
class Secrets(BaseSettings):
    model_config = SettingsConfigDict(
        secrets_dir=SECRETS_DIR, case_sensitive=False, populate_by_name=True
    )
    github_token: str | None = Field(default=None, validation_alias="github-token")
```

`Secrets.require("github_token")` raises `SecretMissing` naming both the expected
file and the environment variable, so a missing token surfaces immediately
rather than as a confusing 403 several calls later.

Non-secret configuration uses the same mechanism with an `ATOMIC_L0G_` prefix,
e.g. `ATOMIC_L0G_DATA_DIR`.

---

## 10. Project layout

```
atomic-l0g/
├── IMPLEMENTATION_PLAN.md
├── pyproject.toml
├── .gitignore                  # .venv/, cache/, data/atomic-l0g.db, .env
├── README.md
├── sources/
│   ├── distros.yaml            # the registry
│   ├── repos.yaml              # tiers + per-repo overrides
│   ├── bots.yaml               # bot/noise patterns per source
│   └── themes.yaml             # keyword taxonomy + is_security label list
├── src/atomic_l0g/
│   ├── __init__.py
│   ├── model.py                # Item / Comment / Release
│   ├── registry.py             # load + validate registry, fail loud
│   ├── settings.py             # pydantic-settings config + /run/secrets
│   ├── http.py                 # clients, pagination, retry, rate guard
│   ├── sync.py                 # target walk, per-target isolation
│   ├── store/
│   │   ├── jsonl.py            # append-only writer, dedup + revision
│   │   └── db.py               # JSONL -> SQLite materialisation + views
│   ├── collectors/
│   │   ├── base.py             # Collector protocol, cursor handling
│   │   ├── github.py
│   │   ├── gitlab.py
│   │   ├── feed.py
│   │   └── structured/
│   │       ├── flatcar.py
│   │       ├── bottlerocket.py
│   │       └── amazonlinux.py
│   ├── analytics/
│   │   ├── cadence.py
│   │   ├── versions.py
│   │   ├── themes.py
│   │   └── lineage.py
│   ├── mcp/
│   │   └── server.py           # read-only tools wrapping the --json CLI
│   └── cli.py
├── deploy/
│   ├── Containerfile           # ubi9-minimal + Python 3.12
│   ├── cronjob.yaml            # al0g-sync, GITHUB_TOKEN from a Secret
│   ├── deployment.yaml         # al0g-mcp, no secrets, store read-only
│   ├── route.yaml              # TLS, host allow-list
│   └── pvc.yaml                # RWX; or omit for the single-pod fallback
├── prompts/
│   └── weekly-digest.md        # reference task; doubles as interface spec
├── data/
│   ├── normalized/
│   ├── cursors/
│   └── annotations/            # never mounted into the served store
└── reports/
```

---

## 11. Phases

### Phase 0 — Scaffold  ✅ done
- `git init`, `pyproject.toml`, `.venv`, `.gitignore`, package skeleton
- `model.py` with `Item` / `Comment` / `Release` + serialisation
- `registry.py` with schema validation
- `sources/*.yaml` skeletons
- `al0g sources validate` implemented

**Done when:** `al0g sources validate` passes on the registry and models round-trip a fixture.
→ 14 projects, 41 repositories.

### Phase 1 — Vertical slice  ✅ done
Flatcar + Bottlerocket through every layer:
- `settings.py` -- pydantic-settings config + `/run/secrets` loading
- `http.py` -- authenticated clients, pagination, rate-limit guard
- `collectors/base.py` -- `Cursor`, `SyncResult`, window + bot helpers
- `collectors/github.py` -- releases, issues, PRs, comments, review comments
- `store/jsonl.py` -- append-only store, dedup, revision tracking
- `store/db.py` -- SQLite materialisation, FTS5, `v_item_signal`
- `sync.py` -- orchestration with per-repository isolation
- CLI: `sync`, `list`, `show`, `fetch --diff`, `db build`

**Done when:** two consecutive syncs produce zero new rows; `fetch --diff` returns a live
diff.
→ Flatcar 4 repos: 274 records in 31s. Bottlerocket 5 repos: 247 records in 16s.
→ Second Flatcar run: `new 0, unchanged 32`. `fetch --diff` returned a live diff.

**Not yet done here:** release *components* (kernel/systemd versions) come from
Flatcar's `releases.json`, which is a Phase 2 structured collector. Phase 1
releases come from the GitHub releases API and carry notes but no components.

### Phase 2 — Breadth
- `feed.py`, `structured/{flatcar,bottlerocket,amazonlinux}.py`
- `gitlab.py` (REST v4 + `GITLAB_TOKEN`)
- Registry filled to all v1 sources; `sources/bots.yaml` populated
- Feed-discovery pass for Talos / Kairos

**Done when:** every distro in §5.1 appears in `al0g list`; no collector reports an
unexplained empty result.

**Progress — 2a done (feeds, search, top):**
- `collectors/feed.py` -- RSS/Atom into `item_kind="blog"`, ids
  `feed:<project>:<label>:<hash12>` so they stay stable across runs. Feeds beat
  the GitHub API on one axis: most supply a real `summary`, the field an agent
  triages on. Summary markup is collapsed to text; `body` keeps the original.
- Feeds are the pseudo-tier `feed`. They need no credentials and consume no API
  budget, so they are collected first — a rate-limited run still gets its blogs.
- `al0g search` (FTS5, `bm25` ranking, user input quoted so `sysext OR` cannot
  break `MATCH`) and `al0g top` (window computed in-query, so `--since 3d`
  works; `--security` filter).
- `is_security` is derived in the index from stored labels plus the registry's
  `security_labels`, not stored on the record: a stored flag could never
  backfill records collected before it existed.
- `sync` prints store and `.git` size, so growth is visible per run.

**Progress — 2b done (CLI vocabulary, storage hidden):**
- `al0g stats` -- per-project counts of new, active, comments and releases.
  NEW (created in window) and ACTIVE (updated in window) are kept apart: the
  difference between a project generating work and one arguing about old work.
- `al0g releases` -- release timeline with a per-project cadence footer.
- `list` gains `--state merged|open|closed` and `--security`.
- Read commands now guarantee their own index freshness: if the index is
  missing or older than the newest JSONL they rebuild it, building to a private
  path and swapping in with `os.replace`. Agents parallelise tool calls, so
  two concurrent reads must not clash on a rebuild.
- `prompts/weekly-digest.md` drives a digest through the CLI alone. The
  interface principle: an agent interacts with the CLI (and later MCP), never
  with the storage. If a prompt needs a filter the CLI lacks, add a command --
  do not expose the schema.

→ 5 parallel cold-start reads all exit 0, one rebuild, no clash.

**Remaining in Phase 2:** `structured/*` (Flatcar release components,
Bottlerocket CHANGELOG, Amazon Linux HTML notes) and `gitlab.py`.

### Phase 3 — Comments + signal + search  ✅ done
- Comment records from GitHub and GitLab
- `signal` frozen fields
- SQLite views `v_item_signal`, FTS5 index over title/summary/body
- CLI: `top`, `search`

**Done when:** "top 10 discussions in the last 7 days" is answered from SQLite with
**zero** network calls.
→ Shipped in Phases 1–2b: comments as first-class records, FTS5 with bm25
ranking, `v_item_signal`, `top`, `search`.

### Phase 4 — Analytics
- `cadence` (releases per week per distro)
- `versions` (kernel / systemd drift across distros)
- `themes` (keyword rollup)
- `lineage` (Flatcar ← CoreOS; ACL ← Flatcar + Azure Linux; Bluefin ← Fedora)

**Done when:** `al0g cadence` shows a real releases/week series and `al0g lineage` renders
the ACL-on-Flatcar relationship.

### Phase 5 — MCP server  ✅ done
Interface: **MCP over Streamable HTTP** (not the deprecated SSE), plus stdio
for local use. One tool per read command.

- **Library**: the official `mcp` SDK, behind an optional extra (`[mcp]`) so the
  CLI still installs without a web stack. `pydantic` is already present via
  `pydantic-settings`, so tool-schema generation costs nothing new.
  Rejected `fastmcp`: heavier still, depends on `mcp` anyway, and its value is
  auth, middleware and hosted deployment, none of which is needed here.
- **Tools are subprocess wrappers** around `al0g ... --json`, returning
  `structuredContent`. Measured CLI overhead is ~220 ms of interpreter and
  `pydantic` import, ~250 ms per call, against LLM latency measured in tens of
  seconds. Shelling out keeps the CLI as the single implementation of every
  query, so the two can never drift. The `--json` contract is what makes this
  a wrapper rather than a rewrite.
- **Not exposed: `sync`.** It writes to the source of truth, advances cursors,
  and spends the GitHub API budget.
- **`fetch` is not exposed on the remote transport.** It makes live API calls
  with the server's credentials; a public, unauthenticated endpoint would let
  any caller drain the 5000 req/hr budget and break collection. Available over
  stdio for local use.
- **Tool descriptions carry the caveats** — which projects have comment data,
  that a zero can mean "never collected" rather than "quiet". This is where
  that knowledge lives, so the prompt shrinks to the task and cannot drift from
  the interface.
- **Code change**: separate the derived index from the store, so a deployment
  can mount the store read-only and keep the index on a writable volume. New
  setting `ATOMIC_L0G_INDEX_PATH` (default `data_dir/atomic-l0g.db`).
  `_ensure_index` then works unchanged: it stats the JSONL and rebuilds into
  the writable index path whenever the store has moved on.

**Done when:** the digest prompt answers from MCP tools alone, with the agent
never invoking the CLI.

**Built.** Notes from doing it, since the plan guessed wrong in one place:

- `FastMCP` was **renamed to `MCPServer`** in SDK 2.x (`mcp.server.mcpserver`).
  The 1.x import path now raises with a pointer to the migration guide. Any
  code written from 1.x examples would have failed on first run, which is what
  the Step 0 API check was for.
- **No second query path was needed.** Each tool shells out to
  `python -m atomic_l0g ... --json`; invoking as a module avoids depending on a
  console script being on `PATH` in a container.
- **The SDK already defaults `enable_dns_rebinding_protection=True`**, so the
  Origin work was configuration, not implementation.
- **Host matching is exact or `host:*` — there is no wildcard.** A fallback
  allow-list of `["*"]` matches no real Host and rejects everything. An unset
  allow-list therefore means localhost only, and a public deployment must name
  its Route hostname.
- **MCP structured content must be an object**, so a returned list gets wrapped
  by the SDK under a generic `result` key. List tools wrap it themselves as
  `{count, items}` instead, which reads better to an agent.
- Limits are **clamped, not rejected** — an agent should not have to retry to
  get an answer. Verified: `limit=9999` returns 200.

Verified: 7 tools over stdio with real data, zero errors; the same over HTTP
with `initialize`/`tools_list`/`call_tool`; `/healthz` returns 200 and is
exempt from transport security; a disallowed Origin is refused with 403 while
an allowed one and an absent one pass.

**Outstanding:** running the digest prompt *against MCP* needs the server
registered in an MCP-capable client, which is the user's side to do.

### Phase 6 — OpenShift deployment
Two workloads, one shared store, and the exposed process holds no credentials.

```
  CronJob  al0g-sync             Deployment  al0g-mcp
  has GITHUB_TOKEN               no secrets at all
  hourly, concurrencyPolicy:     mounts store read-only
    Forbid                       derived index on emptyDir
  writes JSONL + cursors         Service + Route (TLS)
        |                                  ^
        +---------- PVC (RWX) -------------+
             data/normalized, data/cursors
```

- **Secrets.** The collector's `GITHUB_TOKEN` comes from a Secret mounted as a
  file at `/run/secrets/github-token` — already the default location
  `settings.SECRETS_DIR` expects, so no code change is needed. The read server
  mounts nothing, so compromising it leaks no credentials and costs no budget.
- **Storage.** `data/normalized` and `data/cursors` live on a ReadWriteMany PVC
  so the CronJob can write while the server reads. If the cluster has no RWX
  storage class, fall back to a single Deployment running an in-process
  scheduler: one pod, one RWO PVC, no cross-pod sharing.
- **Freshness needs no orchestration.** `_ensure_index` already compares the
  store's mtime against the index on every read, so the server picks up the
  CronJob's writes by itself. Keeping the index on an emptyDir is what allows
  the shared PVC to stay read-only.
- **No auth, but scoped.** Read-only tools only; a server-side ceiling on
  `--limit`; no `sync`; no `fetch`; and **`Origin` header validation** — the
  MCP spec recommends it, because without it a web page can reach the server
  via DNS rebinding. TLS terminates at the Route. Add a per-client request
  budget in middleware, since OpenShift Routes do not rate-limit.
- **The served store is facts only.** `data/annotations/` — the agent-written
  summary layer — is never mounted. That is the one thing in the design that is
  genuinely not public.
- **Image.** `ubi9/ubi-minimal` with the distribution's Python 3.12, not the
  3.14 used locally. `requires-python` stays `>=3.11`; verify the code and the
  `mcp`/`pydantic` wheels against the deployment interpreter before shipping.
  Build with `podman build` or an OpenShift BuildConfig.
- **Probes.** A `/healthz` endpoint distinct from the MCP endpoint, for
  liveness and readiness.

**Done when:** the digest prompt runs against the deployed HTTP endpoint, and
a caller cannot spend the collector's API token.

### Phase 7 — Skill
- Rewrite `coreos-activity` → `image-mode-ecosystem`, pointing at the MCP
  server (or the local CLI) rather than shell recipes
- Keep the weekly digest prompt as the reference task
- `data/annotations/` protocol for agent-written summaries, kept out of the
  served store

**Done when:** the skill answers a 7-day cross-distro question with no live API
calls when the store is fresh.

---

## 12. Verification

There are no unit tests, by choice. Two reasons: the collectors are thin
passthrough over HTTP, so a mocked response mostly tests the mock; and the
properties that actually matter are end-to-end and cheap to observe directly.

Verification is therefore running it live, with two criteria that *are* treated
as non-negotiable:

| Property | How it is checked |
|---|---|
| **Idempotency** | `al0g sync` twice in a row must report `new 0`. This is the load-bearing property of the whole store — if it fails, the JSONL grows without bound and every downstream count is wrong. |
| **Isolation** | A failing repository records an error and the run continues; one broken source never aborts a sync. |
| **Resumability** | Running out of API budget stops cleanly with everything collected so far persisted and the cursor still valid. |
| **Triage invariant** | `sync` reports the count of records with neither title nor summary; it should be zero. |

The `sources validate` command retains strict schema checks for the registry,
which is where genuinely silent failure lives.

---

## 13. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Git bloat | Diffs are never stored. Only normalised JSONL + release notes are committed. |
| Rate limits at "everything" scope | Repo tiers, `since=` incrementality, ETags, cursor resume. First backfill is the expensive run; all later runs are incremental. |
| Schema churn | Phase 1 is one distro through every layer before breadth. |
| Silent source drift | Per-collector health reported on every `sync`. |
| Feed/HTML scraper rot | Scrapers isolated in `structured/`; breakage is visible, not fatal. |
| GitLab auth | Blocked until a PAT exists; GitHub-first path is unaffected. |
| Public endpoint drains the API token | `sync` and `fetch` are never exposed remotely. The MCP server holds no credentials and is read-only, so a caller can neither spend the budget nor mutate the store. |
| Public endpoint serves internal analysis | Only `data/normalized` and `data/cursors` are mounted. `data/annotations` is excluded by design. |
| Unbounded queries on a public endpoint | Server-side ceiling on `--limit`, a per-client request budget in middleware, and no `fetch` (the one tool that would make outbound calls). |
| DNS rebinding against the MCP server | `Origin` header validation, as the MCP spec recommends; TLS and host allow-listing at the Route. |
| Derived index vs read-only store | `ATOMIC_L0G_INDEX_PATH` lets the index live on a writable volume while the store is mounted read-only. |
| Interpreter drift (dev 3.14, deploy 3.12) | `requires-python` stays `>=3.11`; verify code and wheels against the deployment interpreter before shipping. |
| No RWX storage class | Fall back to a single Deployment with an in-process scheduler: one pod, one RWO PVC. |

---

## 14. Prerequisites

| Need | Status |
|---|---|
| Python 3.14 + venv + pip | ✅ verified |
| SQLite with FTS5 + JSON1 | ✅ verified (3.51.2) |
| PyPI reachable | ✅ verified |
| GitHub REST access | ✅ MCP confirmed authenticated as `cverna` |
| **`GITHUB_TOKEN` for Python** | ❌ needed — MCP token is not reusable from Python; unauth is 60 req/hr |
| **`GITLAB_TOKEN`** | ❌ needed (`read_api`); `glab` currently returns 401 |
| `bottlerocket.aws` reachability | ❌ fails here — fall back to `raw.githubusercontent.com` + `releases.atom` |
| AL2023 release notes RSS | ❌ none — HTML scraper required |
| `mcp` SDK on the deployment interpreter | ❌ unverified — confirm wheels for 3.12 (deploy target) and 3.14 (dev) before Phase 5 |
| `podman build` locally | ⚠️ podman 5.8.4 is rootless under a QEMU provider with no `buildah`/`skopeo` binary — confirm a build, or use an OpenShift BuildConfig |
| OpenShift storage class | ❌ unknown — RWX simplifies Phase 6; without it use the single-Deployment fallback |
| Public Route | ❌ to be decided — hostname and TLS termination |

---

## 15. Deliberately out of scope

- PR/MR diffs in the store (fetched on demand via `fetch_ref`)
- LLM-generated summaries in the facts store (they live in `data/annotations/`)
- A web dashboard or UI (agent + markdown reports are the interface)
- Baked-in importance scoring (raw signals are exposed; the agent weighs them)
- Backfilling history before first collection (the store starts now and grows forward)
- **MCP authentication.** There is nothing confidential in the served store. The
  exposure that matters is metered API budget and write paths, and both are
  handled by never exposing `sync` or `fetch` and by keeping credentials out of
  the serving pod — not by authenticating callers.
- **Serving the annotations layer.** If it is ever populated, it stays internal.
