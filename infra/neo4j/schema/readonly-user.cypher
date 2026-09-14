// Read-only user for memory-api and NeoDash. Neo4j Community supports a single database with
// built-in roles; `reader` grants read-only access. Password comes from NEO4J_READONLY_PASSWORD.
// Executed by `aimemory-ingest migrate` against the `system` database with parameter substitution.
CREATE USER $readonly_user IF NOT EXISTS SET PASSWORD $readonly_password CHANGE NOT REQUIRED;
GRANT ROLE reader TO $readonly_user;
