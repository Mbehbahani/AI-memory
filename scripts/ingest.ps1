<#
.SYNOPSIS
  Run an ingestion pass via the `ingestion` service's CLI (`aimemory-ingest run`).

.DESCRIPTION
  Wraps `docker compose exec ingestion aimemory-ingest run --root <root> [--tier N]`. The `ingestion`
  service must already be up (it is an always-on worker per ADR-0011; scripts/up.ps1 starts it).
  Falls back to `docker compose run --rm ingestion` if the worker container is not running.

.PARAMETER Root
  Source root id from config/source-roots.yaml (e.g. "vault", "joblab-de").

.PARAMETER Tier
  Optional tier filter (0, 1, or 2).

.EXAMPLE
  pwsh scripts/ingest.ps1 -Root vault
  pwsh scripts/ingest.ps1 -Root joblab-de -Tier 1
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [ValidateSet(0, 1, 2)][int]$Tier,
    [string[]]$ExtraArgs = @()
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    $cliArgs = @('aimemory-ingest', 'run', '--root', $Root)
    if ($PSBoundParameters.ContainsKey('Tier')) { $cliArgs += @('--tier', $Tier) }
    $cliArgs += $ExtraArgs

    $running = (docker compose ps -q ingestion 2>&1)
    if ($running -and $running.Trim() -ne '') {
        Write-Host "docker compose exec ingestion $($cliArgs -join ' ')" -ForegroundColor Cyan
        & docker compose exec ingestion @cliArgs
    } else {
        Write-Host "ingestion worker not running -- using 'docker compose run --rm'" -ForegroundColor Yellow
        Write-Host "docker compose run --rm ingestion $($cliArgs -join ' ')" -ForegroundColor Cyan
        & docker compose run --rm ingestion @cliArgs
    }
    if ($LASTEXITCODE -ne 0) { throw "ingestion run failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}
