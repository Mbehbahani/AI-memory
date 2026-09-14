# ADR-0007 — All published ports bind to 127.0.0.1; databases exposed only in the dev override

Status: accepted · Date: 2026-09-13

## Decision
`docker-compose.yml` publishes only memory-api (8000), mcp-server (8020), and NeoDash (5005), each as
`127.0.0.1:<port>`. `docker-compose.override.yml` (dev) adds postgres 5432, neo4j 7474/7687, ollama
11434, embedding 8010 — also loopback only. The Docker Desktop default `0.0.0.0` binding is never used.
memory-api and NeoDash connect to Neo4j with a read-only user.

## Consequences
+ Nothing on the LAN can reach the stack. − Remote access (future) needs an explicit design, not a
port change.
