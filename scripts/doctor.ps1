<#
.SYNOPSIS
  Health check for the AI Memory stack: Docker daemon, compose config, per-service health, port
  bindings (loopback-only), model presence, disk space.

.DESCRIPTION
  Read-only. Exits non-zero if a required check fails. Run after scripts/up.ps1.
  -Security runs the A13 security checklist section (stub until P15-T01; A13 fills this in).

.EXAMPLE
  pwsh scripts/doctor.ps1
  pwsh scripts/doctor.ps1 -Security
#>
[CmdletBinding()]
param(
    [switch]$Security
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot

$failures = @()
function Test-Step {
    param([string]$Name, [scriptblock]$Body)
    Write-Host "== $Name ==" -ForegroundColor Cyan
    try {
        & $Body
    } catch {
        Write-Host "FAIL: $Name -- $($_.Exception.Message)" -ForegroundColor Red
        $script:failures += $Name
    }
}

try {
    Test-Step "Docker daemon" {
        $v = docker version --format '{{.Server.Version}}' 2>&1
        if ($LASTEXITCODE -ne 0) { throw "Docker daemon not reachable: $v" }
        Write-Host "Docker Engine $v reachable." -ForegroundColor Green
    }

    Test-Step ".env present" {
        if (-not (Test-Path -LiteralPath (Join-Path $repoRoot '.env'))) {
            throw ".env missing -- run scripts/init-env.ps1"
        }
        Write-Host ".env present." -ForegroundColor Green
    }

    Test-Step "docker compose config" {
        docker compose config --quiet
        if ($LASTEXITCODE -ne 0) { throw "docker compose config failed" }
        Write-Host "Compose files (base + override) are valid." -ForegroundColor Green
    }

    Test-Step "Service status / health" {
        $json = docker compose ps --format json 2>&1
        if ($LASTEXITCODE -ne 0) { throw "docker compose ps failed: $json" }
        $lines = $json -split "`n" | Where-Object { $_.Trim() -ne '' }
        if (-not $lines) { Write-Host "No services running (stack is down)." -ForegroundColor Yellow; return }
        $services = $lines | ForEach-Object { $_ | ConvertFrom-Json }
        foreach ($svc in $services) {
            $name = $svc.Service
            $state = $svc.State
            $health = $svc.Health
            $status = if ($health) { "$state ($health)" } else { $state }
            $color = if ($state -eq 'running' -and ($health -eq 'healthy' -or -not $health)) { 'Green' } else { 'Yellow' }
            Write-Host ("{0,-20} {1}" -f $name, $status) -ForegroundColor $color
            if ($health -eq 'unhealthy') { $script:failures += "service:$name" }
        }
    }

    Test-Step "Published ports are loopback-only (127.0.0.1)" {
        $ids = (docker compose ps -q) -split "`n" | Where-Object { $_.Trim() -ne '' }
        $bad = @()
        foreach ($id in $ids) {
            $ports = docker inspect $id --format '{{json .NetworkSettings.Ports}}' | ConvertFrom-Json
            if (-not $ports) { continue }
            foreach ($prop in $ports.PSObject.Properties) {
                foreach ($binding in $prop.Value) {
                    if ($binding -and $binding.HostIp -and $binding.HostIp -ne '127.0.0.1') {
                        $bad += "$id $($prop.Name) -> $($binding.HostIp):$($binding.HostPort)"
                    }
                }
            }
        }
        if ($bad.Count -gt 0) {
            throw "Non-loopback published port(s): $($bad -join '; ')"
        }
        Write-Host "All published container ports are bound to 127.0.0.1." -ForegroundColor Green
    }

    Test-Step "Host port bindings (netstat)" {
        $expected = @(8000, 8020, 5005, 5432, 7474, 7687, 11434, 8010)
        $listening = netstat -ano | Select-String 'LISTENING'
        foreach ($p in $expected) {
            $hit = $listening | Where-Object { $_ -match "127\.0\.0\.1:$p\s" }
            $hitAny = $listening | Where-Object { $_ -match ":$p\s" }
            if ($hitAny -and -not $hit) {
                Write-Host "Port $p is listening but not confirmed 127.0.0.1-only via netstat (check manually)." -ForegroundColor Yellow
            }
        }
        Write-Host "netstat scan complete (informational; the docker inspect check above is authoritative)." -ForegroundColor Green
    }

    Test-Step "Ollama model presence (qwen3:4b)" {
        $running = docker compose ps -q ollama 2>&1
        if (-not $running -or $running.Trim() -eq '') {
            Write-Host "ollama service not running -- skip." -ForegroundColor Yellow
            return
        }
        $list = docker compose exec -T ollama ollama list 2>&1
        if ($LASTEXITCODE -ne 0) { throw "ollama list failed: $list" }
        if ($list -notmatch 'qwen3:4b') {
            throw "qwen3:4b not present. Run scripts/up.ps1 and wait for ollama-init, or check its logs."
        }
        Write-Host "qwen3:4b present." -ForegroundColor Green
    }

    Test-Step "Disk space" {
        docker system df
        $drive = (Get-Item $repoRoot).PSDrive
        $freeGb = [math]::Round($drive.Free / 1GB, 1)
        Write-Host "Host drive $($drive.Name): $freeGb GB free." -ForegroundColor Green
        if ($freeGb -lt 10) {
            Write-Host "WARNING: less than 10 GB free on $($drive.Name):." -ForegroundColor Yellow
        }
    }

    if ($Security) {
        Write-Host "== Security checklist (--security) ==" -ForegroundColor Cyan
        Write-Host "STUB: full checklist implemented by A13 in P15-T01 (docs/security/*)." -ForegroundColor Yellow
        Write-Host "Placeholder checks only; do not treat this section as a completed security review." -ForegroundColor Yellow
        # A13: add checks here for read-only mounts, secret detector coverage, MCP write-guard
        # defaults, Neo4j read-only user usage by memory-api/NeoDash, sanitized error responses, etc.
    }

    Write-Host ""
    if ($failures.Count -eq 0) {
        Write-Host "doctor: ALL CHECKS PASSED" -ForegroundColor Green
        exit 0
    } else {
        Write-Host "doctor: FAILED checks: $($failures -join ', ')" -ForegroundColor Red
        exit 1
    }
} finally {
    Pop-Location
}
