# Formal evaluation sealed inputs

This directory is intentionally ignored except for this boundary document.
The formal workflow requires the following private, frozen inputs:

- `preregistration.yaml`: schema-v2 model, host, policy, image, seed, and threshold lock;
- `manifest.json`: tracked identifiers and hashes for confirmatory and diagnostic tasks;
- `input-lock.json`: SHA-256 values for every sealed input;
- `agent-tasks.json`: questions, schemas, gold SQL, allowed graphs, and local source paths;
- `pilot-agent-results.json`: sanitized independent-pilot rows used only for power planning;
- `deterministic-suite.json`: raw locked relationship, SQL, taxonomy, and
  performance cases from which the approved workflow generates evidence;
- one or more SQLite or CSV source files referenced by `agent-tasks.json`.

Do not commit raw data, questions, gold SQL, model logs, provider keys, or review
state. `input-lock.json` must cover every sealed file, and the formal preflight
fails on missing files, symlinks, hash drift, split leakage, exact task
duplicates, ambiguous confirmatory truth, or an incomplete model/host matrix.
All database and project paths are bounded relative paths, must resolve inside
the locked input root, and must themselves be covered by `input-lock.json`.
Manifest near-duplicate cluster IDs are recomputed from the sealed question,
schema, and SQL-shape content; caller-supplied arbitrary cluster IDs are rejected.
