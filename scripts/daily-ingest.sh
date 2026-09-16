#!/usr/bin/env bash
#
# Daily ingest: pull new activity into the store.
#
# Incremental by design. The cursors are authoritative, so the window below
# applies only to targets that have never been collected; a run after an outage
# catches up on its own and there is no gap to repair by hand.
#
# Environment-agnostic: works under a systemd timer, cron, or a container
# CronJob. It needs a GitHub token, which the CLI finds either in the
# environment (GITHUB_TOKEN) or at /run/secrets/github-token.
set -euo pipefail

# Resolve the checkout from this script's own location, so the unit file does
# not have to, and so PATH does not matter under systemd.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

AL0G="${AL0G:-$REPO/.venv/bin/al0g}"
DATA_DIR="${ATOMIC_L0G_DATA_DIR:-/var/lib/atomic-l0g}"
WINDOW="${ATOMIC_L0G_WINDOW:-7d}"

if [ ! -x "$AL0G" ]; then
    echo "no al0g at $AL0G -- set AL0G or create the venv in $REPO" >&2
    exit 1
fi

export ATOMIC_L0G_DATA_DIR="$DATA_DIR"
mkdir -p "$DATA_DIR"

# exec, so the CLI's exit status and signal handling are the script's. The CLI
# already fails non-zero when a source errored or credentials were missing,
# which is exactly what a timer needs to go red on.
exec "$AL0G" sync --since "$WINDOW"
