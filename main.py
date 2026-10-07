"""Compatibility entry point for running the MCP server from the repository root."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from expense_mcp.server import main


if __name__ == "__main__":
    main()
