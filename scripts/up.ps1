<#
.SYNOPSIS
  Bring up the AI Memory stack (base compose + dev override, loaded automatically).

.DESCRIPTION
  Wraps `docker compose up -d`. Requires .env (run scripts/init-env.ps1 first). Pass -Viz to also
  start NeoDash (`--profile viz`). Never starts the `tools` profile (use scripts/test.ps1).

.EXAMPLE
  pwsh scripts/up.ps1
  pwsh scripts/up.ps1 -Viz
#>
[CmdletBinding()]
param(
    [switch]$Viz
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot '.env'))) {
        throw ".env not found. Run scripts/init-env.ps1 first."
    }

    $composeArgs = @('compose', 'up', '-d')
    if ($Viz) { $composeArgs = @('compose', '--profile', 'viz', 'up', '-d') }

    Write-Host "docker $($composeArgs -join ' ')" -ForegroundColor Cyan
    & docker @composeArgs
    if ($LASTEXITCODE -ne 0) { throw "docker compose up failed with exit code $LASTEXITCODE" }

    Write-Host "Stack starting. Check status with 'docker compose ps' or scripts/doctor.ps1." -ForegroundColor Green
} finally {
    Pop-Location
}
