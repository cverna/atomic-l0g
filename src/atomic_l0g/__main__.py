"""Entry point for ``python -m atomic_l0g``.

Lets the MCP server invoke the CLI as a module rather than as a console script,
so it does not depend on ``PATH`` being set up inside a container.
"""

from atomic_l0g.cli import app

if __name__ == "__main__":
    app()
