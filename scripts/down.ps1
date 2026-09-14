<#
.SYNOPSIS
  Stop the AI Memory stack WITHOUT deleting volumes. Never passes -v/--volumes to docker compose.

.DESCRIPTION
  Wraps `docker compose down --remove-orphans` (containers + network removed, named volumes kept).
  Hard-refuses to run if the caller tries to sneak a -v/--volumes flag in via -ExtraArgs, since that
  would delete pg_data/neo4j_data/ollama_models/ingestion_state — never do that outside an explicit,
  separately-reviewed volume-reset task.

.EXAMPLE
  pwsh scripts/down.ps1
#>
[CmdletBinding()]
param(
    # Deliberately not documented/encouraged; exists only so the guard below has something to check.
    [string[]]$ExtraArgs = @()
)

$ErrorActionPreference = 'Stop'

foreach ($arg in $ExtraArgs) {
    if ($arg -eq '-v' -or $arg -eq '--volumes') {
        throw "REFUSED: '$arg' would delete named volumes (pg_data, neo4j_data, ollama_models, ingestion_state). Never run 'docker compose down -v' in this project."
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    Write-Host "docker compose down --remove-orphans" -ForegroundColor Cyan
    & docker compose down --remove-orphans
    if ($LASTEXITCODE -ne 0) { throw "docker compose down failed with exit code $LASTEXITCODE" }
    Write-Host "Stack stopped. Volumes preserved (pg_data, neo4j_data, neo4j_logs, neo4j_import, ollama_models, ingestion_state)." -ForegroundColor Green
} finally {
    Pop-Location
}
