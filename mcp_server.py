#!/usr/bin/env python3
"""
OlokaTTS Model Context Protocol (MCP) Server Entrypoint.
Runs the MCP server over standard I/O (stdio) for clients like:
- Claude Desktop
- Cursor
- Antigravity / Gemini CLI
- Windsurf
- Zed

Usage:
    python mcp_server.py

Configuration example for Claude Desktop / Cursor:
    {
      "mcpServers": {
        "olokatts": {
          "command": "python",
          "args": ["<FULL_PATH_TO_VIENEU_GATEWAY>/mcp_server.py"],
          "env": {
            "OLOKATTS_GATEWAY_URL": "https://phucsd-vieneu-gateway.hf.space"
          }
        }
      }
    }
"""

import sys
import os

# Ensure UTF-8 on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.mcp_server import mcp

if __name__ == "__main__":
    mcp.run()
