# Current product and experiment architecture

Status: reconciled on 2026-08-30 against the current Stage 1 Developer Preview
contract and the evaluation ledger. This page describes the current branch; it
does not turn Developer Preview behavior or bounded experiment results into a
general release claim.

Solid arrows are execution paths. Dashed arrows show a dependency or an
explicitly excluded responsibility.

```mermaid
flowchart TB
    U["Natural-language request"] --> A["AI SQL Agent / Harness<br/>selects entities, fields, and target grain"]
    A -.->|"JoinLint does not establish"| GAP["Upstream responsibility<br/>business intent and entity-set completeness"]

    subgraph PRODUCT["Current product: Stage 1 Developer Preview"]
        direction TB

        MCP["Local read-only STDIO MCP<br/>exactly two tools"]
        PLAN["get_join_plan<br/>2–8 entity refs, at most 4 hops"]
        VALIDATE["validate_sql<br/>final SQL plus optional plan_id"]
        SERVICE["Deterministic runtime service"]

        SOURCE["Trusted project-relative SQLite source<br/>explicit or bounded discovery"]
        REL["Authorized relationships<br/>declared foreign keys plus curated model"]
        EVIDENCE["Current exact evidence<br/>uniqueness, cardinality, grain, fan-out"]
        GRAPH["Relationship graph planner"]
        PARSER["SQLGlot parse-only validation<br/>SELECT / WITH"]
        CACHE["Private user cache<br/>sanitized and content-addressed"]
        PROOF["Immutable Join Proof<br/>plan_id plus evidence digest"]
        RESULT["Structured result<br/>scope, findings, recovery guidance<br/>execution_count = 0"]

        CLI["Separate human-governed CLI<br/>scan → candidates → accept<br/>baseline → check"]

        MCP --> PLAN
        MCP --> VALIDATE
        PLAN --> SERVICE
        VALIDATE --> PARSER
        PARSER --> SERVICE

        SOURCE --> REL
        CLI --> REL
        REL --> EVIDENCE
        EVIDENCE --> GRAPH
        GRAPH --> SERVICE
        SERVICE <--> CACHE
        SERVICE --> PROOF
        SERVICE --> RESULT
    end

    A -->|"1. request a Join Proof"| MCP
    PROOF -->|"return proof"| A
    A -->|"2. submit final SQL and plan_id"| VALIDATE
    RESULT -->|"pass, block, or inconclusive"| A

    DBMCP["Separate database MCP<br/>schema inspection and SQL execution"]
    DB["SQLite database"]

    A -->|"inspect schema"| DBMCP
    RESULT -->|"continue only after validation passes"| DBMCP
    DBMCP --> DB
    SOURCE -.->|"JoinLint reads evidence; it never executes SQL"| DB

    BOUNDARY["Explicitly outside JoinLint scope<br/>business semantics, metric definitions, filters,<br/>answer correctness, and complete entity selection"]
    MCP -.->|"not_validated_scope"| BOUNDARY

    subgraph EXPERIMENT["Current experiment architecture: frozen, paired, traceable, fail-closed"]
        direction TB

        TASKS["Frozen tasks and data<br/>trusted query contract when applicable"]
        LOCK["Input lock + run plan + lineage<br/>bind commit, samples, hosts, and model"]
        GUARD["GitHub admission gate<br/>authorization, archive digest, duplicates, environment"]
        BUDGET["Atomic campaign ledger<br/>reserve → consume → settle"]
        INCIDENTS["Evaluation and CI failure ledger<br/>Pattern-Key plus deterministic recurrence guard"]

        RUNNER["Inspect AI harness<br/>GitHub Actions + Modal<br/>fixed Codex / Claude host and model"]
        CONTROL["Control<br/>same task, model, host, and database MCP<br/>without JoinLint"]
        TREATMENT["Treatment<br/>only experimental difference: add current JoinLint MCP"]

        TRACE["Trace and mechanism scoring<br/>plan_called / MCP-grounded / SQL-validated / protocol"]
        ITT["Paired intention-to-treat rows<br/>isolated infrastructure failures remain zeros"]
        STATS["Primary analysis<br/>Join-Correct Task Completion<br/>exact two-sided McNemar test"]
        VERIFY["Independent strict verifier<br/>inventory, digests, and run identity"]
        PUBLIC["Durable sanitized evidence<br/>result docs + Draft Release + public ledger"]
        RAW["Private raw Inspect traces<br/>runner-ephemeral or ignored local preservation"]

        TASKS --> LOCK
        LOCK --> GUARD
        BUDGET --> GUARD
        INCIDENTS --> GUARD
        GUARD --> RUNNER
        RUNNER --> CONTROL
        RUNNER --> TREATMENT
        CONTROL --> TRACE
        TREATMENT --> TRACE
        TRACE --> ITT
        ITT --> STATS
        STATS --> VERIFY
        VERIFY --> PUBLIC
        RUNNER -.-> RAW
    end

    TREATMENT -.->|"uses the same product implementation"| MCP
    DBMCP -.->|"shared dependency"| CONTROL
    DBMCP -.->|"shared dependency"| TREATMENT

    POSITIVE["Supported narrow result<br/>trusted complete contract plus opaque relationships:<br/>control 6/20 → treatment 20/20; p = 0.0001220703125"]
    COUNTER["Constraint evidence<br/>semantic-safety experiments were negative;<br/>local natural BIRD probe was a negative diagnostic"]
    LIMIT["Current claim boundary<br/>no general natural-language SQL, business-semantic,<br/>relationship-discovery, or entity-selection claim"]

    STATS --> POSITIVE
    STATS --> COUNTER
    POSITIVE --> LIMIT
    COUNTER --> LIMIT
```

The experiment treatment reuses the current MCP implementation rather than a
special evaluation-only product. The most important boundary is upstream:
JoinLint can prove relationships among requested physical entities, but it
cannot prove that the Agent selected every entity required by the user's intent.
