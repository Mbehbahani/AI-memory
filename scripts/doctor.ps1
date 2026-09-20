<#
.SYNOPSIS
  Health check for the AI Memory stack: Docker daemon, compose config, per-service health, port
  bindings (loopback-only), model presence, disk space.

.DESCRIPTION
  Read-only. Exits non-zero if a required check fails. Run after scripts/up.ps1.
  -Security runs the plan section T security checklist (A13, P15-T01): read-only mounts, loopback
  exposure, .env hygiene, the ADR-0013 Neo4j limitation, the secret-detector egress control, MCP
  write posture and audit-sink health, sanitized errors, AWS credential exposure, and log hygiene.
  Findings and the full write-up: docs/security/threat-model.md.

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
        # No `2>&1` here: in Windows PowerShell 5.1 redirecting a native command's stderr wraps every
        # line in a NativeCommandError, which made this step report a false FAIL even when the model
        # was present and the exit code was 0 (MEASURED 2026-09-17, A13).
        $list = docker compose exec -T ollama ollama list | Out-String
        if ($LASTEXITCODE -ne 0) { throw "ollama list failed (exit $LASTEXITCODE)" }
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
        Write-Host ""
        Write-Host "== Security checklist (-Security) ==" -ForegroundColor Cyan
        Write-Host "Plan section T, verified live. Full write-up: docs/security/threat-model.md and" -ForegroundColor Gray
        Write-Host "docs/security/checklist-<date>.md. This section checks posture, not code paths -" -ForegroundColor Gray
        Write-Host "tests/integration/test_security_checklist.py is the executable half." -ForegroundColor Gray

        Test-Step "T1 Source roots and credential mounts are read-only" {
            $ids = (docker compose ps -q) -split "`n" | Where-Object { $_.Trim() -ne '' }
            $roDests = @('/sources/vault', '/sources/joblab-de', '/home/app/.aws')
            $bad = @(); $checked = 0
            foreach ($id in $ids) {
                $mounts = docker inspect $id --format '{{json .Mounts}}' | ConvertFrom-Json
                foreach ($m in $mounts) {
                    if ($roDests -contains $m.Destination) {
                        $checked++
                        if ($m.RW) { $bad += "$($m.Destination) is RW in $id" }
                    }
                }
            }
            if ($bad.Count -gt 0) { throw ($bad -join '; ') }
            if ($checked -eq 0) {
                Write-Host "No source-root mounts found (ingestion not running) -- skip." -ForegroundColor Yellow
            } else {
                Write-Host "$checked source/credential mount(s), all RW=false." -ForegroundColor Green
            }
        }

        Test-Step "T2 Base compose publishes only 8000/8020/5005" {
            # --profile viz so NeoDash (5005) is included; without it the profile's service is
            # elided and this check would silently pass on a two-port subset.
            $doc = docker compose -f (Join-Path $repoRoot 'docker-compose.yml') --profile viz --profile tools config --format json | ConvertFrom-Json
            $published = @()
            foreach ($svc in $doc.services.PSObject.Properties) {
                foreach ($p in $svc.Value.ports) {
                    $published += [pscustomobject]@{ Service = $svc.Name; Ip = $p.host_ip; Port = [string]$p.published }
                }
            }
            $offLoopback = $published | Where-Object { $_.Ip -ne '127.0.0.1' }
            if ($offLoopback) {
                throw "not on loopback: $(($offLoopback | ForEach-Object { "$($_.Service) $($_.Ip):$($_.Port)" }) -join '; ')"
            }
            $allowed = @('8000', '8020', '5005')
            $extra = $published | Where-Object { $allowed -notcontains $_.Port }
            if ($extra) {
                throw "base compose publishes unexpected port(s): $(($extra | ForEach-Object { "$($_.Service):$($_.Port)" }) -join '; ')"
            }
            Write-Host "Base compose publishes $((($published | ForEach-Object { $_.Port }) | Sort-Object -Unique) -join ', ') on 127.0.0.1 only." -ForegroundColor Green
        }

        Test-Step "T3 .env is git-ignored and untracked" {
            git -C $repoRoot check-ignore -q .env
            if ($LASTEXITCODE -ne 0) { throw ".env is not covered by .gitignore" }
            # `git ls-files .env` (not `--error-unmatch`) so an untracked .env is an empty result
            # rather than a stderr line PowerShell 5.1 would promote to a terminating error.
            $tracked = git -C $repoRoot ls-files -- .env
            if ($tracked) { throw ".env is TRACKED by git -- rotate every credential in it" }
            Write-Host ".env is ignored and untracked." -ForegroundColor Green
        }

        Test-Step "T4 Neo4j privilege boundary (ADR-0013 -- expected absent)" {
            Write-Host "Neo4j Community has no role-based access control: every authenticated user" -ForegroundColor Yellow
            Write-Host "can write. memory_reader is an identity, NOT a privilege boundary (ADR-0013)." -ForegroundColor Yellow
            Write-Host "What actually holds: client-side write refusal in Neo4jGraphStore.query()," -ForegroundColor Gray
            Write-Host "the graph being rebuildable (ADR-0001), and loopback-only bolt (ADR-0007)." -ForegroundColor Gray
            $api = docker compose ps -q memory-api
            if ($api) {
                $user = docker inspect $api --format '{{range .Config.Env}}{{println .}}{{end}}' |
                    Select-String '^NEO4J_USER=' | ForEach-Object { ($_ -split '=', 2)[1] }
                if (-not $user) { throw "memory-api has no NEO4J_USER set" }
                if ($user.Trim() -ne 'memory_reader') {
                    throw "memory-api connects to Neo4j as '$($user.Trim())', not memory_reader"
                }
                Write-Host "memory-api uses the memory_reader identity." -ForegroundColor Green
            } else {
                Write-Host "memory-api not running -- identity check skipped." -ForegroundColor Yellow
            }
        }

        Test-Step "T5 Secret-flagged sources have no stored text (egress control, ADR-0014)" {
            $pg = docker compose ps -q postgres
            if (-not $pg) { Write-Host "postgres not running -- skip." -ForegroundColor Yellow; return }
            # The database *name* and *role* are not secrets; the password is never read on the host
            # side (psql authenticates over the container's local socket). Passing them as separate
            # argv items avoids `sh -c "...$VAR..."`, which Windows PowerShell 5.1 mangles when it
            # re-quotes arguments for a native executable (MEASURED 2026-09-17, A13).
            $pgEnv = docker inspect $pg --format '{{range .Config.Env}}{{println .}}{{end}}'
            $pgUser = ($pgEnv | Select-String '^POSTGRES_USER=' | ForEach-Object { ($_ -split '=', 2)[1].Trim() })
            $pgDb = ($pgEnv | Select-String '^POSTGRES_DB=' | ForEach-Object { ($_ -split '=', 2)[1].Trim() })
            if (-not $pgUser -or -not $pgDb) { throw "could not read POSTGRES_USER/POSTGRES_DB from the container" }
            $sql = 'SELECT count(*) FROM sources s WHERE s.secret_suspected AND (EXISTS (SELECT 1 FROM source_versions v JOIN source_text t ON t.version_id = v.id WHERE v.source_id = s.id) OR EXISTS (SELECT 1 FROM chunks c WHERE c.source_id = s.id) OR EXISTS (SELECT 1 FROM episodes e WHERE e.source_id = s.id))'
            $leaks = (docker compose exec -T postgres psql -qtAX -U $pgUser -d $pgDb -c $sql | Out-String).Trim()
            if ($LASTEXITCODE -ne 0) { throw "query failed: $leaks" }
            if ($leaks -ne '0') {
                throw "$leaks secret_suspected source(s) carry stored text/chunks/episodes -- content may have reached the LLM provider"
            }
            Write-Host "No secret_suspected source has stored text, chunks or episodes." -ForegroundColor Green
        }

        Test-Step "T6 MCP write posture (ADR-0008)" {
            $mcp = docker compose ps -q mcp-server
            if (-not $mcp) { Write-Host "mcp-server not running -- skip." -ForegroundColor Yellow; return }
            $env = docker inspect $mcp --format '{{range .Config.Env}}{{println .}}{{end}}'
            $forbidden = $env | Select-String '^(DATABASE_URL|POSTGRES_PASSWORD|NEO4J_PASSWORD|NEO4J_URI)='
            if ($forbidden) { throw "mcp-server holds database credentials: $($forbidden -join ', ')" }
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8020/health' -TimeoutSec 10
            Write-Host ("writes effective={0} (mcp={1}, gateway={2}); confirm={3}; {4}/min; {5} chars" -f `
                $health.writes.effective, $health.writes.mcp_write_enabled, $health.writes.gateway_write_enabled, `
                $health.writes.confirm_required, $health.writes.rate_limit_per_minute, $health.writes.max_text_chars) -ForegroundColor Green
            if ($health.writes.effective) {
                Write-Host "WRITES ARE ENABLED. Intentional? See ADR-0008 and docs/operations/mcp.md." -ForegroundColor Yellow
            }
            if (-not $health.writes.confirm_required) { throw "confirm=true is not required" }
            if ($health.audit.degraded) {
                Write-Host ("Audit sink is DEGRADED (sink={0}, dropped={1}). ADR-0008 requires refusals in" -f $health.audit.sink, $health.audit.dropped) -ForegroundColor Yellow
                Write-Host "mcp_audit_log; while degraded they exist only in container logs. Restart mcp-server" -ForegroundColor Yellow
                Write-Host "to re-probe the sink. See docs/security/checklist-2026-09-17.md finding SEC-03." -ForegroundColor Yellow
                $script:failures += 'T6 MCP audit sink degraded'
            } else {
                Write-Host ("Audit sink healthy: {0}/{1} persisted." -f $health.audit.persisted, $health.audit.submitted) -ForegroundColor Green
            }
        }

        Test-Step "T7 Errors are sanitized (no stack trace, no host path)" {
            $body = try {
                (Invoke-WebRequest -Uri 'http://127.0.0.1:8000/v1/entities/not-a-uuid' -SkipHttpErrorCheck -TimeoutSec 10).Content
            } catch { $_.Exception.Message }
            if ($body -match 'Traceback|File "|[A-Za-z]:\\\\|/app/|My-Vault') {
                throw "error response leaks internals: $body"
            }
            Write-Host "Sample 422 body carries no traceback or host path." -ForegroundColor Green
        }

        Test-Step "T8 AWS credential exposure (ADR-0014 / A03 flag)" {
            $ing = docker compose ps -q ingestion
            if (-not $ing) { Write-Host "ingestion not running -- skip." -ForegroundColor Yellow; return }
            $mounts = docker inspect $ing --format '{{json .Mounts}}' | ConvertFrom-Json
            $aws = $mounts | Where-Object { $_.Destination -eq '/home/app/.aws' }
            if (-not $aws) { Write-Host "No ~/.aws mounted -- zero AWS exposure." -ForegroundColor Green; return }
            if ($aws.RW) { throw "the AWS credential mount is READ-WRITE" }
            if ($aws.Source -match 'aws-empty') {
                Write-Host "AWS mount points at the empty default directory -- no real credentials." -ForegroundColor Green
                return
            }
            Write-Host "Host AWS credentials are mounted read-only into ingestion." -ForegroundColor Yellow
            Write-Host "Blast radius = the permissions of the mounted profile, NOT just Bedrock." -ForegroundColor Yellow
            Write-Host "See docs/security/threat-model.md finding SEC-01 (scope a bedrock:InvokeModel-only" -ForegroundColor Yellow
            Write-Host "IAM principal instead of an administrator key). Unset HOST_AWS_DIR and set" -ForegroundColor Yellow
            Write-Host "LLM_PROVIDER=ollama to remove this surface entirely." -ForegroundColor Yellow
        }

        Test-Step "T9 Logs carry no credential and no host path" {
            $logs = docker compose logs --no-color --tail 4000 2>&1 | Out-String
            $hits = [regex]::Matches($logs, 'AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|aws_secret_access_key|[A-Za-z]:\\\\Users|My-Vault')
            if ($hits.Count -gt 0) { throw "$($hits.Count) credential/host-path pattern(s) in the last 4000 log lines" }
            Write-Host "Last 4000 log lines carry no credential pattern and no host path." -ForegroundColor Green
        }
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
