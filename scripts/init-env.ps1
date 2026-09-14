<#
.SYNOPSIS
  Create .env from .env.example with cryptographically random passwords for
  POSTGRES_PASSWORD, NEO4J_PASSWORD, NEO4J_READONLY_PASSWORD. Never prints the generated values.

.DESCRIPTION
  Run once before `scripts/up.ps1`. Safe to re-run: by default it refuses to overwrite an existing
  .env; pass -Force to regenerate (this rotates all three passwords and rewrites .env, so update any
  already-running containers/volumes accordingly — Postgres/Neo4j only pick up a changed password on
  first init of an empty volume).

.EXAMPLE
  pwsh scripts/init-env.ps1
  pwsh scripts/init-env.ps1 -Force
#>
[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$examplePath = Join-Path $repoRoot '.env.example'
$envPath = Join-Path $repoRoot '.env'

if (-not (Test-Path -LiteralPath $examplePath)) {
    throw ".env.example not found at $examplePath"
}

if ((Test-Path -LiteralPath $envPath) -and -not $Force) {
    Write-Host ".env already exists at $envPath -- not overwriting. Re-run with -Force to regenerate." -ForegroundColor Yellow
    exit 0
}

function New-RandomSecret {
    param([int]$Length = 32)
    # Alphanumeric only: the value is embedded in a URI (DATABASE_URL), so no reserved/URI-special
    # characters (@:/?#[]% etc.) that could break parsing.
    # Uses RNGCryptoServiceProvider (not the .NET 6+ static RandomNumberGenerator.GetBytes) for
    # compatibility with Windows PowerShell 5.1 (.NET Framework).
    $chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
    $bytes = New-Object byte[] $Length
    $rng = [System.Security.Cryptography.RNGCryptoServiceProvider]::new()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}

$lines = Get-Content -LiteralPath $examplePath

# First pass: read the (non-secret) user/db names so DATABASE_URL can be rebuilt consistently.
$postgresUser = 'aimemory'
$postgresDb = 'aimemory'
foreach ($line in $lines) {
    if ($line -match '^POSTGRES_USER=(.*)$') { $postgresUser = $Matches[1] }
    if ($line -match '^POSTGRES_DB=(.*)$') { $postgresDb = $Matches[1] }
}

$postgresPassword = New-RandomSecret -Length 32
$neo4jPassword = New-RandomSecret -Length 32
$neo4jReadonlyPassword = New-RandomSecret -Length 32

$outLines = foreach ($line in $lines) {
    if ($line -match '^POSTGRES_PASSWORD=') {
        "POSTGRES_PASSWORD=$postgresPassword"
    } elseif ($line -match '^NEO4J_PASSWORD=') {
        "NEO4J_PASSWORD=$neo4jPassword"
    } elseif ($line -match '^NEO4J_READONLY_PASSWORD=') {
        "NEO4J_READONLY_PASSWORD=$neo4jReadonlyPassword"
    } elseif ($line -match '^DATABASE_URL=') {
        "DATABASE_URL=postgresql+psycopg://${postgresUser}:${postgresPassword}@postgres:5432/${postgresDb}"
    } else {
        $line
    }
}

# Windows PowerShell 5.1's Set-Content has no "utf8NoBOM" option (that's PS 6+); write via .NET so
# the .env file has no BOM (docker compose's env-file parser is happier without one).
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllLines($envPath, $outLines, $utf8NoBom)

Write-Host "Wrote $envPath" -ForegroundColor Green
Write-Host "Generated random POSTGRES_PASSWORD / NEO4J_PASSWORD / NEO4J_READONLY_PASSWORD (values not printed)." -ForegroundColor Green
Write-Host "Before 'scripts/up.ps1', review HOST_VAULT_ROOT / HOST_PILOT_ROOT and other non-secret values in .env." -ForegroundColor Yellow
