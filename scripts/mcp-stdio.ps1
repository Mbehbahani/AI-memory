<#
.SYNOPSIS
  stdio launcher for the AI Memory MCP server.

.DESCRIPTION
  MCP clients that can only spawn a subprocess (Claude Desktop, some editors) speak stdio, while the
  server itself runs in the mcp-server container on 127.0.0.1:8020 over streamable HTTP. This script
  is the bridge: it runs apps/mcp-server/stdio_proxy.py *inside* that container and hands its stdin
  and stdout to the client, so the host needs nothing but Docker - no Python, no mcp SDK.

  It is a pump, not a second server. Every call still goes through the one running server, so the
  ADR-0008 write gate, the 10/min rate limit and the audit trail apply identically to a stdio client
  and to an HTTP client.

  Nothing but MCP traffic may be written to stdout. Diagnostics go to stderr, as MCP requires.

  Prefer the HTTP transport when the client supports it (Claude Code does):
      claude mcp add --transport http ai-memory http://127.0.0.1:8020/mcp

.PARAMETER Url
  The in-container URL of the server. Default http://localhost:8020/mcp.

.EXAMPLE
  pwsh scripts/mcp-stdio.ps1

.EXAMPLE
  # .mcp.json / claude_desktop_config.json entry
  # "ai-memory": {
  #   "command": "pwsh",
  #   "args": ["-NoProfile", "-File", "D:/AI memory/scripts/mcp-stdio.ps1"]
  # }
#>
[CmdletBinding()]
param(
    [string]$Url = 'http://localhost:8020/mcp'
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    # -T disables TTY allocation, which is what makes stdin/stdout a clean binary pipe.
    # -w sets the working directory, because apps/mcp-server holds a hyphen and is therefore not
    #    an importable package (see apps/mcp-server/main.py).
    $composeArgs = @(
        'compose', 'exec', '-T',
        '-w', '/app/apps/mcp-server',
        'mcp-server', 'python', 'stdio_proxy.py', $Url
    )
    & docker @composeArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
