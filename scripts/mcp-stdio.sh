#!/usr/bin/env bash
# stdio launcher for the AI Memory MCP server.
#
# MCP clients that can only spawn a subprocess speak stdio, while the server runs in the mcp-server
# container on 127.0.0.1:8020 over streamable HTTP. This script bridges the two by running
# apps/mcp-server/stdio_proxy.py inside that container and handing its stdin/stdout to the client -
# so the host needs nothing but Docker.
#
# It is a pump, not a second server: the ADR-0008 write gate, the 10/min rate limit and the audit
# trail apply identically to stdio and HTTP clients.
#
# stdout carries MCP traffic only; diagnostics go to stderr.
#
# Prefer HTTP where the client supports it:
#   claude mcp add --transport http ai-memory http://127.0.0.1:8020/mcp
#
# Usage: scripts/mcp-stdio.sh [url]      (default http://localhost:8020/mcp)
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

url="${1:-http://localhost:8020/mcp}"

# -T keeps stdin/stdout a clean pipe (no TTY); -w is needed because apps/mcp-server contains a
# hyphen and so cannot be an importable Python package (see apps/mcp-server/main.py).
exec docker compose exec -T -w /app/apps/mcp-server mcp-server python stdio_proxy.py "$url"
