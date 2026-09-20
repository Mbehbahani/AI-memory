<#
.SYNOPSIS
  Run the P14-T03 gold-set evaluation (tests/evaluation/run_eval.py) in the `tools` profile image.

.DESCRIPTION
  Queries the live Memory Gateway (`memory-api`, reached from `tools` over the compose network as
  `http://memory-api:8000`) with `tests/evaluation/gold.yaml` and writes reports/evaluation-<ts>.md.
  Requires the compose stack to be up (`scripts/up.ps1`).

  --benchmark/--model (ADR-0010 §3, P14-T05's frozen `tests/evaluation/benchmark/` set) is not
  implemented by this wrapper or by run_eval.py yet - that is separate, not-yet-built work; this
  script always runs the gold set, never the frozen benchmark.

.PARAMETER EvalArgs
  Extra args forwarded to run_eval.py (e.g. -EvalArgs "--no-expand", -EvalArgs "--k","10").

.EXAMPLE
  pwsh scripts/eval.ps1
  pwsh scripts/eval.ps1 -EvalArgs "--no-expand"
#>
[CmdletBinding()]
param(
    [string[]]$EvalArgs = @()
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    $cmd = @('python', 'tests/evaluation/run_eval.py') + $EvalArgs
    Write-Host "docker compose --profile tools run --rm tools $($cmd -join ' ')" -ForegroundColor Cyan
    & docker compose --profile tools run --rm tools @cmd
    $code = $LASTEXITCODE
    if ($code -ne 0) { Write-Host "eval: FAILED (exit $code)" -ForegroundColor Red } else { Write-Host "eval: done" -ForegroundColor Green }
    exit $code
} finally {
    Pop-Location
}
