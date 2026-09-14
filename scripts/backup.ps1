<#
.SYNOPSIS
  Back up everything that cannot be trivially recreated (plan section AA): PostgreSQL (system of
  record), .env, config/, .memoryignore, and the NeoDash dashboard if it exists.

.DESCRIPTION
  PostgreSQL: `pg_dump -Fc` run *inside* the postgres container using the container's own
  POSTGRES_PASSWORD/POSTGRES_USER/POSTGRES_DB env vars (never read, printed, or passed through this
  script's own process arguments), written to backups/postgres/<timestamp>.dump.
  Everything else is copied verbatim into backups/config/<timestamp>/.
  Neo4j and embeddings are NOT backed up here -- they are rebuildable (scripts/rebuild-graph.ps1,
  re-embed from source_text). Ollama models are re-pulled, not backed up.

.EXAMPLE
  pwsh scripts/backup.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    if (-not (Test-Path -LiteralPath '.env')) { throw ".env not found" }

    $ids = (docker compose ps -q postgres 2>&1)
    if (-not $ids -or $ids.Trim() -eq '') { throw "postgres service is not running -- run scripts/up.ps1 first" }

    $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $pgDir = Join-Path $repoRoot 'backups\postgres'
    $cfgDir = Join-Path $repoRoot "backups\config\$timestamp"
    New-Item -ItemType Directory -Force -Path $pgDir | Out-Null
    New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null
    $dumpPath = Join-Path $pgDir "$timestamp.dump"

    Write-Host "Running pg_dump -Fc inside the postgres container -> $dumpPath" -ForegroundColor Cyan

    # Use the postgres container's OWN env vars (already set for image bootstrap) so the password
    # never has to be read, printed, or passed as an argument from this script/host process.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'docker'
    foreach ($a in @('compose', 'exec', '-T', 'postgres', 'sh', '-c',
            'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"')) {
        $psi.ArgumentList.Add($a)
    }
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false

    $proc = [System.Diagnostics.Process]::Start($psi)
    $outFile = [System.IO.File]::Create($dumpPath)
    $proc.StandardOutput.BaseStream.CopyTo($outFile)
    $outFile.Close()
    $stderr = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()

    if ($proc.ExitCode -ne 0) {
        Remove-Item -LiteralPath $dumpPath -ErrorAction SilentlyContinue
        throw "pg_dump failed (exit $($proc.ExitCode)): $stderr"
    }
    $size = (Get-Item -LiteralPath $dumpPath).Length
    if ($size -eq 0) { throw "pg_dump produced an empty file -- check container logs" }
    Write-Host "Postgres dump: $dumpPath ($size bytes)" -ForegroundColor Green

    Copy-Item -LiteralPath '.env' -Destination (Join-Path $cfgDir '.env')
    Copy-Item -LiteralPath '.memoryignore' -Destination (Join-Path $cfgDir '.memoryignore') -ErrorAction SilentlyContinue
    Copy-Item -Path 'config' -Destination (Join-Path $cfgDir 'config') -Recurse -ErrorAction SilentlyContinue

    $dashboard = Join-Path $repoRoot 'infra\neodash\dashboard.json'
    if (Test-Path -LiteralPath $dashboard) {
        Copy-Item -LiteralPath $dashboard -Destination (Join-Path $cfgDir 'dashboard.json')
        Write-Host "NeoDash dashboard included." -ForegroundColor Green
    } else {
        Write-Host "NeoDash dashboard not present yet (infra/neodash/dashboard.json) -- skipped." -ForegroundColor Yellow
    }

    Write-Host "Config snapshot: $cfgDir" -ForegroundColor Green
    Write-Host "Backup complete." -ForegroundColor Green
} finally {
    Pop-Location
}
