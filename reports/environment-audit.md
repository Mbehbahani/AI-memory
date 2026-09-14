# Environment Audit — re-run at START DEVELOPMENT

Task: **P0-T01** · Agent: A00 (orchestrator) · Date: **2026-09-14** · All values MEASURED unless labelled.

## Commands executed

```text
docker version --format '{{.Server.Version}}'
docker compose version
docker info --format '{{.OperatingSystem}} | CPUs={{.NCPU}} | Mem={{.MemTotal}} | Containers={{.Containers}} | Images={{.Images}}'
Get-CimInstance Win32_OperatingSystem / Win32_Processor ; Get-PSDrive -PSProvider FileSystem
Get-NetTCPConnection -State Listen  (ports 5432 7474 7687 11434 8000 8010 8020 5005)
git --version ; wsl -l -v
ls "D:/My-Vault" ; ls "D:/AWS2/SupaBaseProject/DE"
```

## Host (MEASURED 2026-09-14)

| Item | Result |
|---|---|
| OS | Microsoft Windows 11 Pro 10.0.26200 |
| CPU | 11th Gen Intel Core i7-1165G7 @ 2.80 GHz — 4 cores / 8 threads |
| RAM | 31.7 GB total, 15.6 GB free at audit |
| Disk C: | 131.3 GB used / 146.7 GB free |
| Disk D: | 12.5 GB used / 182.8 GB free |
| Git | 2.55.0.windows.3 |
| GPU | Intel Iris Xe only, no NVIDIA (DOCUMENTED from 2026-09-13 audit; unchanged) |

## Docker (MEASURED 2026-09-14)

| Item | Result |
|---|---|
| Docker Engine (server) | 29.7.2 — **daemon reachable** |
| Docker Compose | v5.4.0 |
| Docker Desktop VM | 8 CPUs, 16,596,459,520 bytes (15.46 GiB) memory available to containers |
| Containers / Images at start | 0 / 0 (clean slate) |
| WSL | `wsl -l -v` reports distro `docker-desktop` in state *Stopped* while the daemon is reachable — Docker Desktop 29.7 is not using a classic WSL2 distro for the engine on this host. No `.wslconfig` (AC-9 declined). Container memory ceiling is therefore the 15.46 GiB reported by `docker info`, which covers the ESTIMATED 8–11 GB ingest-time stack. |

## Ports (MEASURED 2026-09-14) — all free

`5432 free · 7474 free · 7687 free · 11434 free · 8000 free · 8010 free · 8020 free · 5005 free`

## Source roots (MEASURED 2026-09-14) — both readable

| Root | Top-level entries observed |
|---|---|
| `D:\My-Vault` (vault) | `00 Inbox`, `01 Projects`, `02 Areas`, `03 Resources`, `04 Archives`, `05 Templates`, `06 Outputs`, `07 Workflows`, `AIOS`, `Clippings`, `Excalidraw`, `AGENTS.md`, `CLAUDE.md`, `START HERE.md`, `prompt`, `2026-05-20.md` |
| `D:\AWS2\SupaBaseProject\DE` (joblab-de, pilot) | `docs`, `src`, `tests`, `data`, `Tableau`, `tools-cache`, `README.md`, `AGENTS.md`, `pyproject.toml`, `requirements-dev.lock`, `LICENSE`, `Book5-tableau.twb(x)` |

`tools-cache/` and the bundled JDK are IGNORE per §K; `Book5-tableau.twbx` and `data/` fall to
CATALOG_ONLY by size/type rules.

## Verdict

**P0-T01 PASS.** Docker daemon reachable, all eight ports free, both source roots readable,
no port or disk blocker. Development proceeds to P1.

## Owner decisions in force (from the START DEVELOPMENT message)

AC-1 … AC-8 accepted as recommended · AC-9 **no** (no `.wslconfig` cap) · AC-10 **no** (no scheduled
task; on-demand runs only) · AC-11 accepted · AC-12 accepted.
