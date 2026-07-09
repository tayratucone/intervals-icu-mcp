"""HTTP MCP entry point for ChatGPT connectors."""

import os

from .server import mcp


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    path = os.getenv("MCP_PATH", "/mcp")
    if not path.startswith("/"):
        path = f"/{path}"
    mcp.run(transport="http", host=host, port=port, path=path)


if __name__ == "__main__":
    main()
