<#
.SYNOPSIS
  Restore a PostgreSQL backup produced by scripts/backup.ps1.

.DESCRIPTION
  Runs `pg_restore --clean --if-exists` inside the postgres container against an existing (already
  migrated) database, using the container's own POSTGRES_USER/POSTGRES_DB/POSTGRES_PASSWORD env vars.
  Does NOT touch Neo4j (rebuild it afterwards with scripts/rebuild-graph.ps1) or config files (copy
  those back from backups/config/<timestamp>/ by hand -- restoring .env automatically would be
  destructive to the running stack's current secrets without a deliberate decision).

.PARAMETER DumpFile
  Path to a .dump file, or a bare timestamp (e.g. "20260914-120000") to resolve under
  backups/postgres/.

.EXAMPLE
  pwsh scripts/restore.ps1 -DumpFile 20260914-120000
  pwsh scripts/restore.ps1 -DumpFile backups/postgres/20260914-120000.dump
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$DumpFile
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    if (-not (Test-Path -LiteralPath '.env')) { throw ".env not found" }

    $path = $DumpFile
    if (-not (Test-Path -LiteralPath $path)) {
        $candidate = Join-Path $repoRoot "backups\postgres\$DumpFile.dump"
        if (Test-Path -LiteralPath $candidate) { $path = $candidate }
    }
    if (-not (Test-Path -LiteralPath $path)) { throw "Dump file not found: $DumpFile" }

    $ids = (docker compose ps -q postgres 2>&1)
    if (-not $ids -or $ids.Trim() -eq '') { throw "postgres service is not running -- run scripts/up.ps1 first" }

    Write-Host "About to run pg_restore --clean --if-exists from $path" -ForegroundColor Yellow
    Write-Host "This will DROP and recreate objects in the current database. Ctrl+C within 5s to abort." -ForegroundColor Yellow
    Start-Sleep -Seconds 5

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'docker'
    foreach ($a in @('compose', 'exec', '-T', 'postgres', 'sh', '-c',
            'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore --clean --if-exists -U "$POSTGRES_USER" -d "$POSTGRES_DB"')) {
        $psi.ArgumentList.Add($a)
    }
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false

    $proc = [System.Diagnostics.Process]::Start($psi)
    $inStream = [System.IO.File]::OpenRead($path)
    $inStream.CopyTo($proc.StandardInput.BaseStream)
    $proc.StandardInput.BaseStream.Close()
    $inStream.Close()
    $stderr = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()

    if ($proc.ExitCode -ne 0) { throw "pg_restore failed (exit $($proc.ExitCode)): $stderr" }
    if ($stderr) { Write-Host $stderr -ForegroundColor Yellow }

    Write-Host "Restore complete from $path." -ForegroundColor Green
    Write-Host "Neo4j was not touched -- run scripts/rebuild-graph.ps1 to re-project it from Postgres." -ForegroundColor Yellow
} finally {
    Pop-Location
}
