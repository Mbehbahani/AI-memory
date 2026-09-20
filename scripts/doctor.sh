#!/usr/bin/env bash
# Health check for the AI Memory stack: Docker daemon, compose config, per-service health, port
# bindings (loopback-only), model presence, disk space. Read-only.
# Usage: scripts/doctor.sh [--security]
# --security runs the plan section T checklist (A13, P15-T01): read-only mounts, loopback exposure,
# .env hygiene, the ADR-0013 Neo4j limitation, the secret-detector egress control, MCP write posture
# and audit-sink health, sanitized errors, AWS credential exposure, log hygiene.
# Full write-up and findings: docs/security/threat-model.md
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
fail=0

step() { echo "== $1 =="; }

step "Docker daemon"
if ! docker version --format '{{.Server.Version}}' >/tmp/doctor_docker_version 2>&1; then
    echo "FAIL: Docker daemon not reachable: $(cat /tmp/doctor_docker_version)"
    fail=1
else
    echo "Docker Engine $(cat /tmp/doctor_docker_version) reachable."
fi

step ".env present"
if [[ ! -f .env ]]; then
    echo "FAIL: .env missing -- run scripts/init-env.sh"
    fail=1
else
    echo ".env present."
fi

step "docker compose config"
if ! docker compose config --quiet; then
    echo "FAIL: docker compose config failed"
    fail=1
else
    echo "Compose files (base + override) are valid."
fi

step "Service status / health"
docker compose ps || true

step "Published ports are loopback-only (127.0.0.1)"
bad=0
for id in $(docker compose ps -q 2>/dev/null); do
    ports="$(docker inspect "$id" --format '{{json .NetworkSettings.Ports}}')"
    if echo "$ports" | grep -q '"HostIp":"0.0.0.0"'; then
        echo "FAIL: container $id publishes a port on 0.0.0.0"
        bad=1
    fi
done
if [[ "$bad" -eq 1 ]]; then fail=1; else echo "All published container ports are bound to 127.0.0.1."; fi

step "Ollama model presence (qwen3:4b)"
if docker compose ps -q ollama >/dev/null 2>&1 && [[ -n "$(docker compose ps -q ollama)" ]]; then
    if docker compose exec -T ollama ollama list 2>&1 | grep -q 'qwen3:4b'; then
        echo "qwen3:4b present."
    else
        echo "FAIL: qwen3:4b not present."
        fail=1
    fi
else
    echo "ollama service not running -- skip."
fi

step "Disk space"
docker system df || true
df -h "$repo_root" || true

if [[ "${1:-}" == "--security" ]]; then
    echo ""
    echo "== Security checklist (--security) =="
    echo "Plan section T, verified live. Full write-up: docs/security/threat-model.md and"
    echo "docs/security/checklist-<date>.md. tests/integration/test_security_checklist.py is the"
    echo "executable half; this section checks the posture of the running stack."

    step "T1 Source roots and credential mounts are read-only"
    ro_bad=0
    ro_checked=0
    for id in $(docker compose ps -q 2>/dev/null); do
        while IFS='|' read -r dest rw; do
            case "$dest" in
                /sources/vault|/sources/joblab-de|/home/app/.aws)
                    ro_checked=$((ro_checked + 1))
                    if [[ "$rw" == "true" ]]; then
                        echo "FAIL: $dest is mounted read-write in $id"
                        ro_bad=1
                    fi
                    ;;
            esac
        done < <(docker inspect "$id" --format '{{range .Mounts}}{{.Destination}}|{{.RW}}{{"\n"}}{{end}}')
    done
    if [[ "$ro_bad" -eq 1 ]]; then
        fail=1
    elif [[ "$ro_checked" -eq 0 ]]; then
        echo "No source-root mounts found (ingestion not running) -- skip."
    else
        echo "$ro_checked source/credential mount(s), all RW=false."
    fi

    step "T2 Base compose publishes only 8000/8020/5005 on 127.0.0.1"
    # `docker compose config` normalises every port to the long form, so `host_ip:` always precedes
    # the `published:` it belongs to - which is what makes this awk pairing correct.
    # `--profile viz` so NeoDash (5005) is included; without it the profile's service is elided and
    # the check would silently pass on a two-port subset.
    base_ports="$(docker compose -f docker-compose.yml --profile viz --profile tools config 2>/dev/null \
        | awk '/^[[:space:]]*host_ip:[[:space:]]/ {ip=$2}
               /^[[:space:]]*published:[[:space:]]/ {gsub(/"/, "", $2); print ip "|" $2}')"
    if [[ -z "$base_ports" ]]; then
        echo "WARN: could not read the base compose ports -- skip."
    else
        port_bad=0
        seen_ports=""
        while IFS='|' read -r ip port; do
            [[ -z "$port" ]] && continue
            seen_ports="$seen_ports $port"
            if [[ "$ip" != "127.0.0.1" ]]; then
                echo "FAIL: base compose publishes $port on '${ip:-<any>}', not 127.0.0.1"
                port_bad=1
            fi
            case "$port" in
                8000|8020|5005) ;;
                *) echo "FAIL: base compose publishes unexpected port $port (section T allows 8000/8020/5005 only)"; port_bad=1 ;;
            esac
        done <<< "$base_ports"
        if [[ "$port_bad" -eq 1 ]]; then
            fail=1
        else
            echo "Base compose publishes$seen_ports on 127.0.0.1 only."
        fi
    fi

    step "T3 .env is git-ignored and untracked"
    if ! git check-ignore -q .env; then
        echo "FAIL: .env is not covered by .gitignore"
        fail=1
    elif git ls-files --error-unmatch .env >/dev/null 2>&1; then
        echo "FAIL: .env is TRACKED by git -- rotate every credential in it"
        fail=1
    else
        echo ".env is ignored and untracked."
    fi

    step "T4 Neo4j privilege boundary (ADR-0013 -- expected absent)"
    echo "Neo4j Community has no role-based access control: every authenticated user can write."
    echo "memory_reader is an identity, NOT a privilege boundary (ADR-0013). What actually holds:"
    echo "client-side write refusal in Neo4jGraphStore.query(), a rebuildable graph (ADR-0001),"
    echo "and loopback-only bolt (ADR-0007)."
    api_id="$(docker compose ps -q memory-api 2>/dev/null)"
    if [[ -n "$api_id" ]]; then
        neo_user="$(docker inspect "$api_id" --format '{{range .Config.Env}}{{println .}}{{end}}' \
            | sed -n 's/^NEO4J_USER=//p' | tr -d '\r')"
        if [[ "$neo_user" == "memory_reader" ]]; then
            echo "memory-api uses the memory_reader identity."
        else
            echo "FAIL: memory-api connects to Neo4j as '${neo_user:-<unset>}', not memory_reader"
            fail=1
        fi
    else
        echo "memory-api not running -- identity check skipped."
    fi

    step "T5 Secret-flagged sources have no stored text (egress control, ADR-0014)"
    if [[ -n "$(docker compose ps -q postgres 2>/dev/null)" ]]; then
        leaks="$(docker compose exec -T postgres sh -c 'psql -qtAX -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM sources s WHERE s.secret_suspected AND (EXISTS (SELECT 1 FROM source_versions v JOIN source_text t ON t.version_id = v.id WHERE v.source_id = s.id) OR EXISTS (SELECT 1 FROM chunks c WHERE c.source_id = s.id) OR EXISTS (SELECT 1 FROM episodes e WHERE e.source_id = s.id))"' 2>&1 | tr -d '[:space:]')"
        if [[ "$leaks" == "0" ]]; then
            echo "No secret_suspected source has stored text, chunks or episodes."
        else
            echo "FAIL: $leaks secret_suspected source(s) carry stored text/chunks/episodes -- content may have reached the LLM provider"
            fail=1
        fi
    else
        echo "postgres not running -- skip."
    fi

    step "T6 MCP write posture and audit sink (ADR-0008)"
    mcp_id="$(docker compose ps -q mcp-server 2>/dev/null)"
    if [[ -n "$mcp_id" ]]; then
        creds="$(docker inspect "$mcp_id" --format '{{range .Config.Env}}{{println .}}{{end}}' \
            | grep -cE '^(DATABASE_URL|POSTGRES_PASSWORD|NEO4J_PASSWORD|NEO4J_URI)=' || true)"
        if [[ "$creds" != "0" ]]; then
            echo "FAIL: mcp-server holds database credentials (ADR-0008 says it must not)"
            fail=1
        else
            echo "mcp-server holds no database credentials."
        fi
        health="$(curl -sS --max-time 10 http://127.0.0.1:8020/health 2>/dev/null || true)"
        if [[ -n "$health" ]]; then
            # Each of these keys appears exactly once in the /health document.
            field() { echo "$health" | grep -oE "\"$1\": *[^,}]+" | head -1 | sed 's/.*: *//; s/"//g'; }
            echo "writes effective=$(field effective) (mcp=$(field mcp_write_enabled), gateway=$(field gateway_write_enabled));" \
                 "confirm=$(field confirm_required); $(field rate_limit_per_minute)/min; $(field max_text_chars) chars"
            [[ "$(field effective)" == "true" ]] && \
                echo "NOTE: WRITES ARE ENABLED. Intentional? See ADR-0008 and docs/operations/mcp.md."
            if [[ "$(field confirm_required)" != "true" ]]; then
                echo "FAIL: confirm=true is not required"
                fail=1
            fi
            if [[ "$(field degraded)" == "true" ]]; then
                echo "FAIL: audit sink DEGRADED (sink=$(field sink), dropped=$(field dropped)). ADR-0008"
                echo "requires refusals in mcp_audit_log; while degraded they live only in container logs."
                echo "Restart mcp-server to re-probe the sink. See docs/security/checklist-2026-09-17.md"
                echo "finding SEC-03."
                fail=1
            else
                echo "Audit sink healthy: $(field persisted)/$(field submitted) persisted."
            fi
        else
            echo "mcp-server /health unreachable -- skip."
        fi
    else
        echo "mcp-server not running -- skip."
    fi

    step "T7 Errors are sanitized (no stack trace, no host path)"
    body="$(curl -sS --max-time 10 http://127.0.0.1:8000/v1/entities/not-a-uuid 2>/dev/null || true)"
    if [[ -z "$body" ]]; then
        echo "memory-api unreachable -- skip."
    elif echo "$body" | grep -qE 'Traceback|File "|[A-Za-z]:\\\\|/app/|My-Vault'; then
        echo "FAIL: error response leaks internals: $body"
        fail=1
    else
        echo "Sample 422 body carries no traceback or host path."
    fi

    step "T8 AWS credential exposure (ADR-0014 / A03 flag)"
    ing_id="$(docker compose ps -q ingestion 2>/dev/null)"
    if [[ -z "$ing_id" ]]; then
        echo "ingestion not running -- skip."
    else
        aws_line="$(docker inspect "$ing_id" \
            --format '{{range .Mounts}}{{if eq .Destination "/home/app/.aws"}}{{.Source}}|{{.RW}}{{end}}{{end}}')"
        if [[ -z "$aws_line" ]]; then
            echo "No ~/.aws mounted -- zero AWS exposure."
        elif [[ "${aws_line#*|}" == "true" ]]; then
            echo "FAIL: the AWS credential mount is READ-WRITE"
            fail=1
        elif [[ "$aws_line" == *aws-empty* ]]; then
            echo "AWS mount points at the empty default directory -- no real credentials."
        else
            echo "NOTE: host AWS credentials are mounted read-only into ingestion."
            echo "Blast radius = the permissions of the mounted profile, NOT just Bedrock."
            echo "See docs/security/threat-model.md finding SEC-01 (scope a bedrock:InvokeModel-only"
            echo "IAM principal). Unset HOST_AWS_DIR and set LLM_PROVIDER=ollama to remove it entirely."
        fi
    fi

    step "T9 Logs carry no credential and no host path"
    log_hits="$(docker compose logs --no-color --tail 4000 2>/dev/null \
        | grep -cE 'AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|aws_secret_access_key|[A-Za-z]:\\\\Users|My-Vault' || true)"
    if [[ "$log_hits" != "0" ]]; then
        echo "FAIL: $log_hits credential/host-path pattern(s) in the last 4000 log lines"
        fail=1
    else
        echo "Last 4000 log lines carry no credential pattern and no host path."
    fi
fi

echo ""
if [[ "$fail" -eq 0 ]]; then
    echo "doctor: ALL CHECKS PASSED"
    exit 0
else
    echo "doctor: FAILED"
    exit 1
fi
