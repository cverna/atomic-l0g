"""MCP server exposing atomic-l0g to agents.

The CLI is the single implementation of every query.  Each tool below shells
out to ``python -m atomic_l0g ... --json`` and returns the parsed result, so
nothing duplicates query logic and the two surfaces cannot drift.  A new
question is answered by adding a CLI command, not a second code path.

Two commands deliberately have no tool:

* ``sync`` writes to the source of truth and spends the collection API budget.
* ``fetch`` makes live API calls with whatever credentials the process holds.

A deployed server is given no credentials at all, so neither is reachable from
an agent even if the tool existed.

The tool descriptions are load-bearing.  They carry the caveats that would
otherwise live in a prompt -- which projects have comment data, that a zero can
mean "never collected" -- so the knowledge sits with the interface and cannot
drift away from it.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from typing import Any, Optional

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations
    from starlette.requests import Request
    from starlette.responses import JSONResponse
except ModuleNotFoundError as exc:  # pragma: no cover - install guidance
    raise SystemExit(
        "the MCP server needs the optional extra:\n"
        "    python -m pip install 'atomic-l0g[mcp]'"
    ) from exc

from atomic_l0g import __version__
from atomic_l0g.registry import load_registry

__all__ = ["build_server", "main"]

log = logging.getLogger("atomic_l0g.mcp")

#: Invoke the CLI as a module rather than a console script so the server does
#: not depend on PATH inside a container.
CLI_MODULE = "atomic_l0g"

DEFAULT_TIMEOUT = 60.0

#: Ceilings so a public endpoint cannot be asked for unbounded result sets.
#: Requests above the cap are clamped, not rejected -- an agent should not have
#: to retry to get an answer.
MAX_LIMIT = 200
MAX_SEARCH_LIMIT = 100

#: Every tool is a read over local data.  Saying so lets a client reason about
#: safety without guessing.
READ_ONLY = ToolAnnotations(
    read_only_hint=True, idempotent_hint=True, open_world_hint=False
)

INSTRUCTIONS_BODY = """\
atomic-l0g consolidates the image-based Linux ecosystem -- competitor distros
plus the CoreOS baseline -- into a local store. The data is already collected;
these tools answer questions about it.

Before concluding that a project is quiet, check ecosystem_sources. A
repository that has never been collected looks identical to an idle one in
every other tool. In particular `bootc` is registered but collects nothing,
because its repositories are on GitLab and there is no GitLab collector yet.

Known gaps -- state these rather than paper over them:

  - GitLab is not collected, so fedora/bootc is invisible here.
  - Releases carry notes but no component versions, so kernel and systemd
    drift cannot be compared across projects.
  - Comments exist only for core-tier projects (Flatcar, Bottlerocket,
    Azure Linux, Amazon Linux, CoreOS, RHCOS). Watch-tier projects (Talos,
    Kairos, openSUSE MicroOS, Rancher Elemental) show zero comments because
    they are not collected -- missing data, not silence.
  - Bot-authored comments are excluded, so automated activity is absent.
  - openshift/os tracks most work in Jira, so a quiet issue feed there is
    expected.
"""


def _instructions() -> str:
    """Coverage line computed from the registry, plus the fixed caveats.

    The counts used to be written into the text by hand, and a deployment
    promptly reported "39 GitHub repositories ... across 14 registered
    projects" long after it was 43 and 17. A number that has to be maintained
    by hand will be wrong again, so derive it.
    """
    try:
        registry = load_registry(None)
    except (FileNotFoundError, ValueError) as exc:
        log.warning("cannot read the registry for the coverage line: %s", exc)
        return INSTRUCTIONS_BODY

    feeds = sum(len(project.feeds) for project in registry.projects.values())
    covered = sum(1 for repo in registry.repo_tiers if "/" in repo)
    coverage = (
        f"Coverage: {len(registry.projects)} projects, {covered} repositories, "
        f"{feeds} feeds."
    )
    return coverage + "\n\n" + INSTRUCTIONS_BODY

def _clamp(value: int, maximum: int) -> int:
    return max(1, min(int(value), maximum))


def _repeat(flag: str, values: Optional[list[str]]) -> list[str]:
    argv: list[str] = []
    for value in values or []:
        argv += [flag, value]
    return argv


def _items(rows: list[Any]) -> dict[str, Any]:
    """Wrap a list result.

    MCP structured content must be an object, so a bare list gets wrapped by
    the SDK under a generic ``result`` key.  Naming the key and carrying a
    count makes the payload self-describing instead.
    """
    return {"count": len(rows), "items": rows}


def _run(argv: list[str], timeout: float = DEFAULT_TIMEOUT) -> Any:
    """Run the CLI and return its parsed JSON.

    The CLI writes its index-rebuild notice to stderr, so stdout stays pure
    JSON and nothing has to be stripped here.

    Failures raise :class:`ToolError`, not a bare exception: the SDK treats a
    deliberate ToolError as an is_error result carrying its message, whereas
    anything else is treated as a crash and the message is withheld from the
    client.  An agent that asked for a bad id should be told which id was bad,
    not shown "Error executing tool".
    """
    command = [sys.executable, "-m", CLI_MODULE, *argv]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(
            f"atomic-l0g {' '.join(argv)} timed out after {timeout:.0f}s"
        ) from exc

    if completed.returncode != 0:
        # The rebuild notice shares stderr with real errors; drop it so the
        # message the agent sees is the reason, not bookkeeping.
        lines = [
            line
            for line in (completed.stderr or completed.stdout or "").splitlines()
            if line.strip() and not line.startswith("index rebuilt:")
        ]
        detail = " ".join(lines).strip() or "no output"
        raise ToolError(detail)

    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ToolError(
            f"atomic-l0g {' '.join(argv)} returned non-JSON output"
        ) from exc


def build_server() -> MCPServer:
    """Construct the MCP server and register the read-only tools."""
    server = MCPServer(
        name="atomic-l0g",
        title="Image-mode Linux ecosystem",
        version=__version__,
        instructions=_instructions(),
    )

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse({"status": "ok", "version": __version__})

    @server.tool(
        name="ecosystem_stats",
        annotations=READ_ONLY,
        description=(
            "Activity counts over a window: how many items were created, how "
            "many were touched, how much discussion, how many releases. "
            "Start here to see who is busy and who is shipping. "
            "NEW counts items created inside the window; ACTIVE counts items "
            "updated inside it -- the difference between a project generating "
            "work and one still arguing about old work. "
            "Set by='repo' for a per-repository breakdown, which is where the "
            "work actually is: a project total hides one busy repository "
            "behind nine quiet ones. "
            "Returns {since, window, by, groups}, each group carrying its own "
            "key ('distro' or 'repo') plus new, active, comments and releases. "
            "Comment counts exist only for core-tier projects (Flatcar, "
            "Bottlerocket, Azure Linux, Amazon Linux, CoreOS, RHCOS)."
        ),
    )
    def ecosystem_stats(
        since: str = "7d",
        by: str = "distro",
        distro: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Activity counts by project, or per repository with by='repo'."""
        return _run(
            [
                "stats",
                "--since",
                since,
                "--by",
                by,
                *_repeat("--distro", distro),
                "--json",
            ]
        )

    @server.tool(
        name="ecosystem_top",
        annotations=READ_ONLY,
        description=(
            "Rank items by recent discussion activity inside a window. The best "
            "starting point for 'what is being argued about'. Only comments "
            "created inside the window count, so long-running threads are "
            "ranked by current heat rather than lifetime volume. "
            "Comment data exists only for core-tier projects (Flatcar, "
            "Bottlerocket, Azure Linux, Amazon Linux, CoreOS, RHCOS); a "
            "watch-tier project showing zero means comments are not collected, "
            "not that nobody is talking. "
            "Set security=true for items carrying a security label, which today "
            "only Azure Linux and Flatcar apply consistently."
        ),
    )
    def ecosystem_top(
        since: str = "7d",
        distro: Optional[list[str]] = None,
        kind: Optional[list[str]] = None,
        security: bool = False,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Items with the most recent comments, highest first."""
        rows = _run(
            [
                "top",
                "--since",
                since,
                *_repeat("--distro", distro),
                *_repeat("--kind", kind),
                *(["--security"] if security else []),
                "--limit",
                str(_clamp(limit, MAX_LIMIT)),
                "--json",
            ]
        )
        return _items(rows)

    @server.tool(
        name="ecosystem_list",
        annotations=READ_ONLY,
        description=(
            "List collected items -- issues, pull requests and blog posts -- "
            "most recently updated first. "
            "kind is one of: issue, pr, blog. "
            "state is one of: open, closed, merged (use state=merged for 'what "
            "landed'). "
            "Releases are not items; use ecosystem_releases for those."
        ),
    )
    def ecosystem_list(
        since: str = "7d",
        distro: Optional[list[str]] = None,
        kind: Optional[list[str]] = None,
        state: Optional[list[str]] = None,
        security: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Issues, pull requests and blog posts, newest activity first."""
        rows = _run(
            [
                "list",
                "--since",
                since,
                *_repeat("--distro", distro),
                *_repeat("--kind", kind),
                *_repeat("--state", state),
                *(["--security"] if security else []),
                "--limit",
                str(_clamp(limit, MAX_LIMIT)),
                "--json",
            ]
        )
        return _items(rows)

    @server.tool(
        name="ecosystem_releases",
        annotations=READ_ONLY,
        description=(
            "Release timeline, newest first, with citable ids and a per-project "
            "cadence footer. This is how to answer 'who shipped, and how "
            "often'. "
            "Release notes mention component versions in prose but there is no "
            "structured version field, so cross-project kernel or systemd "
            "comparison is not possible from this data."
        ),
    )
    def ecosystem_releases(
        since: str = "90d",
        distro: Optional[list[str]] = None,
        channel: Optional[str] = None,
        limit: int = 40,
    ) -> dict[str, Any]:
        """Release timeline plus a per-project count for the window."""
        argv = [
            "releases",
            "--since",
            since,
            *_repeat("--distro", distro),
            *(["--channel", channel] if channel else []),
            "--limit",
            str(_clamp(limit, MAX_LIMIT)),
            "--json",
        ]
        return _run(argv)

    @server.tool(
        name="ecosystem_search",
        annotations=READ_ONLY,
        description=(
            "Full-text search across the titles, summaries and bodies of "
            "everything collected, ranked by relevance. Use this to follow one "
            "theme -- sysext, bootc, UKI, composefs, confidential computing -- "
            "across every project at once, which is hard to see any other way. "
            "Returns a snippet showing where the match landed."
        ),
    )
    def ecosystem_search(
        query: str,
        since: Optional[str] = None,
        distro: Optional[list[str]] = None,
        kind: Optional[list[str]] = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search everything collected. Quote phrases; bare terms are ANDed."""
        argv = [
            "search",
            query,
            *_repeat("--distro", distro),
            *_repeat("--kind", kind),
            *(["--since", since] if since else []),
            "--limit",
            str(_clamp(limit, MAX_SEARCH_LIMIT)),
            "--json",
        ]
        return _items(_run(argv))

    @server.tool(
        name="ecosystem_show",
        annotations=READ_ONLY,
        description=(
            "Return one full record by id, for citing or reading in detail. "
            "Id shapes: "
            "github:<owner>/<repo>:issue:<number>, "
            "github:<owner>/<repo>:pr:<number>, "
            "github:<owner>/<repo>:release:<version>, "
            "feed:<project>:<label>:<hash>. "
            "Use ecosystem_releases or an ecosystem_top/list/search result to "
            "obtain an id."
        ),
    )
    def ecosystem_show(item_id: str) -> dict[str, Any]:
        """One complete record, including body and signal counts."""
        return _run(["show", item_id, "--json"])

    @server.tool(
        name="ecosystem_sources",
        annotations=READ_ONLY,
        description=(
            "Show one project's repositories, feeds, tiers, and when each was "
            "last collected. "
            "Check this BEFORE concluding a project is quiet: a repository that "
            "has never been collected is indistinguishable from an idle one in "
            "every other tool. A 'never' last-collected means no data, not no "
            "activity. "
            "`bootc` is the standing example -- registered, but never "
            "collected, because its repositories are on GitLab."
        ),
    )
    def ecosystem_sources(project: str) -> dict[str, Any]:
        """One project's repositories and feeds with last-collected times."""
        return _run(["sources", "show", project, "--json"])

    @server.tool(
        name="ecosystem_comments",
        annotations=READ_ONLY,
        description=(
            "Read the stored comments on one item, oldest first. This turns a "
            "comment count into an understanding of what a discussion is "
            "actually about: ecosystem_top and ecosystem_stats tell you which "
            "threads are hot, this tells you what they say. Use it before "
            "characterising an argument -- a thread with 27 comments may be "
            "CI re-triggers rather than design debate, and the bodies are the "
            "only way to know. "
            "Reads the LOCAL STORE: no network access and no credentials, and "
            "it returns exactly the comments the activity counts are built "
            "from, with bot-authored comments already excluded. Safe to call "
            "on a shared endpoint. "
            "Comment data exists only for core-tier projects (Flatcar, "
            "Bottlerocket, Azure Linux, Amazon Linux, CoreOS, RHCOS). A "
            "watch-tier project returns nothing, which means 'not collected', "
            "not 'no discussion'. "
            "The item id comes from an ecosystem_top, ecosystem_list, "
            "ecosystem_search or ecosystem_releases result."
        ),
    )
    def ecosystem_comments(
        item_id: str,
        since: Optional[str] = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Stored comments on one item, oldest first."""
        argv = [
            "comments",
            item_id,
            "--limit",
            str(_clamp(limit, MAX_LIMIT)),
            "--json",
        ]
        if since:
            argv += ["--since", since]
        return _run(argv)

    return server


def _csv_env(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def _transport_security() -> Any:
    """Host and Origin validation for the HTTP transport.

    The SDK matches exactly, or against a ``host:*`` pattern -- there is no
    true wildcard.  A fallback of ``["*"]`` would therefore match no real Host
    at all and reject everything, so an unset allow-list means "localhost
    only" and a public deployment must name its Route hostname.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    configured_hosts = _csv_env("ATOMIC_L0G_ALLOWED_HOSTS")
    configured_origins = _csv_env("ATOMIC_L0G_ALLOWED_ORIGINS")

    if not configured_hosts:
        print(
            "warning: ATOMIC_L0G_ALLOWED_HOSTS is unset, so only localhost is "
            "accepted. Set it to the Route hostname for a public deployment.",
            file=sys.stderr,
        )

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=configured_hosts
        or ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*"],
        # An absent Origin is allowed by the middleware, which covers
        # non-browser MCP clients. These entries cover local browser testing.
        allowed_origins=configured_origins
        or ["http://localhost:*", "http://127.0.0.1:*"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="atomic-l0g-mcp",
        description="Serve atomic-l0g's read-only tools over MCP.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=os.environ.get("ATOMIC_L0G_MCP_TRANSPORT", "stdio"),
        help="stdio for a local client, streamable-http for a shared endpoint.",
    )
    parser.add_argument(
        "--host", default=os.environ.get("ATOMIC_L0G_MCP_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("ATOMIC_L0G_MCP_PORT", "8000"))
    )
    parser.add_argument(
        "--json-response",
        action="store_true",
        help="Return plain JSON instead of SSE streams over HTTP.",
    )
    args = parser.parse_args()

    server = build_server()

    if args.transport == "stdio":
        server.run(transport="stdio")
        return

    server.run(
        transport="streamable-http",
        host=args.host,
        port=args.port,
        json_response=args.json_response,
        transport_security=_transport_security(),
    )


if __name__ == "__main__":
    main()
