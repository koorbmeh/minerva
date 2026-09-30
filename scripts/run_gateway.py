"""Launcher for the Minerva MCP gateway — what Hermes's mcp_servers config invokes.
Adds src/ to sys.path so no install/venv is needed, then runs the gateway."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from minerva_core.gateway.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
