"""Load infra/neodash/dashboard.json into Neo4j so NeoDash's standalone mode can find it.

Why this exists: NeoDash's `standalone` mode (used by the `neodash` compose service, profile `viz`)
does not read a dashboard.json file directly. On connect it runs
    MATCH (d:_Neodash_Dashboard) WHERE d.title = $name RETURN d.content ORDER BY d.date DESC LIMIT 1
against Neo4j itself (compose env `standaloneDashboardName: "AI Memory"`), where `content` is the
whole dashboard JSON serialized to a string on a `:_Neodash_Dashboard` node. There is no file mount
for this in docker-compose.yml (confirmed: the `neodash` service has no volumes). So after any edit to
dashboard.json, re-run this script to push the new content into Neo4j before reloading the NeoDash tab.
(Verified against `neo4jlabs/neodash:2.4.11`'s bundled JS — the query text above was extracted directly
from the shipped bundle, 2026-09-17.)

This is a one-time / after-edit setup step, not a runtime read path, so it uses the full NEO4J_USER
credential (not memory_reader) to write the `:_Neodash_Dashboard` node.

Usage (from a container that already has the `neo4j` Python driver — memory-api or ingestion do):
    docker cp infra/neodash/dashboard.json <container>:/tmp/dashboard.json
    docker cp infra/neodash/load-dashboard.py <container>:/tmp/load-dashboard.py
    docker exec -e NEO4J_URI=bolt://neo4j:7687 -e NEO4J_USER=... -e NEO4J_PASSWORD=... \
        <container> python /tmp/load-dashboard.py /tmp/dashboard.json
"""

import json
import os
import sys
import uuid
from datetime import datetime, timezone

from neo4j import GraphDatabase

uri = os.environ["NEO4J_URI"]
user = os.environ["NEO4J_USER"]
password = os.environ["NEO4J_PASSWORD"]
path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/dashboard.json"

with open(path, "r", encoding="utf-8") as f:
    dashboard = json.load(f)
content = json.dumps(dashboard)
title = dashboard["title"]

driver = GraphDatabase.driver(uri, auth=(user, password))
with driver.session(database="neo4j") as session:
    session.run(
        "MERGE (n:_Neodash_Dashboard {title: $title}) "
        "SET n.uuid = coalesce(n.uuid, $uuid), n.version = $version, n.user = $user, "
        "n.content = $content, n.date = datetime($date)",
        title=title,
        uuid=str(uuid.uuid4()),
        version=dashboard.get("version", "2.4"),
        user="A11-visualization",
        content=content,
        date=datetime.now(timezone.utc).isoformat(),
    )
    rec = session.run(
        "MATCH (n:_Neodash_Dashboard {title: $title}) "
        "RETURN n.uuid AS uuid, n.title AS title, size(n.content) AS content_len, toString(n.date) AS date",
        title=title,
    ).single()
    print(dict(rec))
driver.close()
