# Deploying atomic-l0g on a single VM

One host runs two things: a **writer** that collects on a timer, and a
**reader** that serves MCP over HTTP. They are separate processes so the
network-facing one can be given no credentials.

```
  atomic-l0g-ingest.timer ──> atomic-l0g-ingest.service   (has GITHUB_TOKEN)
        daily 06:00, Persistent=true          writes /var/lib/atomic-l0g
                                                        │
  atomic-l0g-mcp.service  <──────────────────────────────┘
        streamable HTTP :8000, NO credentials    reads the same directory
```

Paths used throughout:

| | |
|---|---|
| `/opt/atomic-l0g` | the checkout and its venv |
| `/var/lib/atomic-l0g` | the store: `normalized/`, `cursors/`, the index |
| `/etc/atomic-l0g/env` | `GITHUB_TOKEN`, mode 0640 |

The store lives in `/var/lib`, **not** inside the checkout, so the daily run
never dirties the git working tree. Git stays the code repo, not the data repo.

### Why these paths

The units assume `/opt/atomic-l0g` and a dedicated `atomic-l0g` service user.
The alternative — checkout in a home directory, running as that user — sets up
faster and works fine, but it means the process reachable from the internet
runs as an account with a shell and usually an SSH key.

If you take the home-directory route anyway, change **four** things in *both*
units: `User`/`Group`, `Documentation`, `ExecStart`, and
`ATOMIC_L0G_DATA_DIR`. That last one is the easy one to miss — the built-in
default resolves relative to the installed package, not to your checkout, so
leaving it unset silently reads a different store than you expect.

---

## 1. Service user

```bash
sudo useradd --system --home-dir /opt/atomic-l0g --shell /sbin/nologin atomic-l0g
```

## 2. Code

Copy the tree **without** the venv, `.git` and the derived index:

```bash
sudo mkdir -p /opt/atomic-l0g
sudo rsync -a --exclude '.venv/' --exclude '.git/' --exclude 'data/' \
  --exclude '__pycache__/' --exclude '*.pyc' \
  ./ /opt/atomic-l0g/
sudo chown -R atomic-l0g: /opt/atomic-l0g
```

## 3. Python environment

As the service user, so ownership is right from the start. Nothing here needs
`gcc` or `python3-devel` — every dependency ships manylinux wheels.

```bash
sudo -u atomic-l0g python3 -m venv /opt/atomic-l0g/.venv
sudo -u atomic-l0g /opt/atomic-l0g/.venv/bin/python -m pip install --upgrade pip
sudo -u atomic-l0g /opt/atomic-l0g/.venv/bin/python -m pip install -e '/opt/atomic-l0g[mcp]'
```

`[mcp]` is required for the server. Without it `atomic-l0g-mcp` cannot start,
because the `mcp` SDK is an optional extra.

Confirm:

```bash
sudo -u atomic-l0g /opt/atomic-l0g/.venv/bin/al0g sources validate
```

## 4. Token

Only the ingest service reads this. Never add it to the MCP unit.

```bash
sudo install -d -m 0750 -o root -g atomic-l0g /etc/atomic-l0g
sudo tee /etc/atomic-l0g/env >/dev/null <<'EOF'
GITHUB_TOKEN=replace-me
EOF
sudo chown root:atomic-l0g /etc/atomic-l0g/env
sudo chmod 0640 /etc/atomic-l0g/env
```

The CLI accepts the token from this environment variable or from a file at
`/run/secrets/github-token`; either works, and the environment file is the
simpler choice on a VM.

## 5. Store

```bash
sudo install -d -m 0750 -o atomic-l0g -g atomic-l0g /var/lib/atomic-l0g
sudo rsync -a ./data/normalized ./data/cursors /var/lib/atomic-l0g/
sudo chown -R atomic-l0g: /var/lib/atomic-l0g
```

Carrying `normalized/` and `cursors/` over means the new host inherits the
existing history and resumes incrementally instead of re-fetching it. The
`atomic-l0g.db` index is deliberately left behind — it is derived, it can be
large, and it is rebuilt on first read.

## 6. Units

```bash
sudo cp /opt/atomic-l0g/deploy/systemd/*.service \
        /opt/atomic-l0g/deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now atomic-l0g-ingest.timer atomic-l0g-mcp.service
```

**Edit `atomic-l0g-mcp.service` before exposing it.** `ATOMIC_L0G_ALLOWED_HOSTS`
ships unset, so the server accepts localhost only and fails closed — any other
`Host` header is rejected with **421** and no explanation. Set it to the
address clients actually use; the list is exact-match or `host:*`, there is no
wildcard.

## 7. Firewall

```bash
sudo firewall-cmd --state                      # often inactive on Fedora Cloud
sudo firewall-cmd --add-port=8000/tcp --permanent && sudo firewall-cmd --reload
```

The security group also needs TCP 8000 ingress from wherever clients are.

## 8. Verify

```bash
systemctl list-timers atomic-l0g-ingest.timer   # next run, and that Persistent is set
sudo systemctl start atomic-l0g-ingest.service  # run the ingest once, now
journalctl -u atomic-l0g-ingest -n 30

curl -s localhost:8000/healthz                  # {"status":"ok",...}
curl -s -X POST localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2025-06-18","capabilities":{},
                 "clientInfo":{"name":"curl","version":"1"}}}'
```

The second curl should return the server's instructions and a `tools`
capability. Expect **8 tools**.

---

## Bootstrap versus daily

The timer runs `al0g sync --since 7d`, but cursors are authoritative, so the
window only matters for a target that has never been collected. Two cases:

**Fresh host, no store.** One wide run first, then let the timer take over:

```bash
sudo -u atomic-l0g bash -c 'set -a; . /etc/atomic-l0g/env; set +a
  ATOMIC_L0G_DATA_DIR=/var/lib/atomic-l0g \
  /opt/atomic-l0g/.venv/bin/al0g sync --since 90d'
```

**A target added later** gets only 7 days on first sight. Widen it:

```bash
... al0g sync --backfill 90d --distro <project>
```

`--since` on its own will *not* do this — on an already-collected target the
cursor wins, and the CLI says so rather than collecting nothing quietly.

## Upgrading

```bash
sudo rsync -a --exclude '.venv/' --exclude '.git/' --exclude 'data/' \
  --exclude '__pycache__/' --exclude '*.pyc' ./ /opt/atomic-l0g/
sudo chown -R atomic-l0g: /opt/atomic-l0g
sudo -u atomic-l0g /opt/atomic-l0g/.venv/bin/python -m pip install -e '/opt/atomic-l0g[mcp]'
sudo systemctl restart atomic-l0g-mcp.service
```

Re-run the `pip install` even for a pure code change: an editable install
points at the tree, but new console scripts or entry points need the metadata
refreshed.

**Adding an MCP tool requires restarting the server.** The tool list is read
once at startup, so a running process will keep serving the old set.

## Hardening (optional)

The units already set `NoNewPrivileges` and `PrivateTmp`. Stronger confinement
for the MCP server, which is the exposed one:

```ini
ProtectSystem=strict
ReadWritePaths=/var/lib/atomic-l0g
PrivateDevices=true
ProtectHome=true
Environment=PYTHONDONTWRITEBYTECODE=1
```

`ReadWritePaths` is needed because the server rebuilds its index when the store
changes; `PYTHONDONTWRITEBYTECODE` avoids warnings when the read-only `/opt`
blocks `.pyc` writes.

Also consider moving `ATOMIC_L0G_ALLOWED_HOSTS` and the token into systemd
credentials or `LoadCredential=` rather than a world-readable-ish env file.

## Troubleshooting

| Symptom | Cause |
|---|---|
| **421 Misdirected Request** | `Host` header not in `ATOMIC_L0G_ALLOWED_HOSTS`. Exact match or `host:*` only. |
| **Connection refused** | Nothing listening, or bound to `127.0.0.1` because `--host 0.0.0.0` is missing. |
| **Connection timed out** | Dropped in transit — security group or firewalld. Not a server problem. |
| Ingest exits 1, "missing secret" | Token absent or expired. This is deliberate: an unattended run must not report success while collecting nothing. |
| Service can't read the store | SELinux. `sudo ausearch -m avc -ts recent`, then `sudo restorecon -Rv /var/lib/atomic-l0g`. |
| `part` in the ingest summary | One phase failed but the rest of the repository was collected. The reason is printed under the row. |
| `kairos-io/kairos` reports `part` on releases, repeatedly | GitHub intermittently 504s or drops that repository's releases endpoint. Confirmed transient rather than payload size: the identical request succeeds minutes later at `per_page=50`. Shrinking the page would cost release history for no reliable gain, so the retry plus phase isolation is the fix — the cost is that repo's releases alone. |

A useful check when a project looks quiet:

```bash
sudo -u atomic-l0g env ATOMIC_L0G_DATA_DIR=/var/lib/atomic-l0g \
  /opt/atomic-l0g/.venv/bin/al0g sources show <project>
```

A repository that has never been collected is indistinguishable from an idle
one everywhere else.
