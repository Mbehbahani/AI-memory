<#
.SYNOPSIS
  Run the test suite in the `tools` profile image (Python 3.12, matches the runtime containers).

.DESCRIPTION
  The host runs Python 3.14, which aimemory does not target (pyproject.toml pins ">=3.12,<3.13"), so
  tests always run inside the `tools` container. Requires postgres/neo4j to be healthy first (see
  docker-compose.override.yml `tools` service depends_on); run scripts/up.ps1 beforehand for
  integration-marked tests, or pass -Unit to skip those.

.PARAMETER Unit
  Run only unit tests (`-m "not integration and not memory and not evaluation and not slow"`).

.PARAMETER PytestArgs
  Extra args forwarded to pytest (e.g. -PytestArgs "-k","test_foo").

.EXAMPLE
  pwsh scripts/test.ps1
  pwsh scripts/test.ps1 -Unit
  pwsh scripts/test.ps1 -PytestArgs "-k","policies"
#>
[CmdletBinding()]
param(
    [switch]$Unit,
    [string[]]$PytestArgs = @()
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    $cmd = @('pytest', '-q')
    if ($Unit) { $cmd += @('-m', 'not integration and not memory and not evaluation and not slow') }
    $cmd += $PytestArgs

    Write-Host "docker compose --profile tools run --rm tools $($cmd -join ' ')" -ForegroundColor Cyan
    & docker compose --profile tools run --rm tools @cmd
    $code = $LASTEXITCODE
    if ($code -ne 0) { Write-Host "tests: FAILED (exit $code)" -ForegroundColor Red } else { Write-Host "tests: PASSED" -ForegroundColor Green }
    exit $code
} finally {
    Pop-Location
}
