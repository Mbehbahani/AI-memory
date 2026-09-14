<#
.SYNOPSIS
  Recreate the Neo4j projection from PostgreSQL (the system of record).

.DESCRIPTION
  Neo4j is a rebuildable projection (plan §AA): if it needs to be reset, wipe its volume separately
  (never via this script -- never `docker compose down -v`) then run this to re-project from
  PostgreSQL via `aimemory-ingest rebuild-graph`.

.EXAMPLE
  pwsh scripts/rebuild-graph.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    $running = (docker compose ps -q ingestion 2>&1)
    if ($running -and $running.Trim() -ne '') {
        Write-Host "docker compose exec ingestion aimemory-ingest rebuild-graph" -ForegroundColor Cyan
        & docker compose exec ingestion aimemory-ingest rebuild-graph
    } else {
        Write-Host "docker compose run --rm ingestion aimemory-ingest rebuild-graph" -ForegroundColor Cyan
        & docker compose run --rm ingestion aimemory-ingest rebuild-graph
    }
    if ($LASTEXITCODE -ne 0) { throw "rebuild-graph failed with exit code $LASTEXITCODE" }
    Write-Host "Graph rebuild complete." -ForegroundColor Green
} finally {
    Pop-Location
}
