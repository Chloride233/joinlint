# Current product and experiment architecture

Status: reconciled on 2026-08-30 against the current Stage 1 Developer Preview
contract and the evaluation ledger. This page describes the current branch; it
does not turn Developer Preview behavior or bounded experiment results into a
general release claim.

Solid arrows are execution paths. Dashed arrows show a dependency or an
explicitly excluded responsibility.

## Product runtime

```mermaid
flowchart TB
    U["Natural-language request"] --> A["AI SQL Agent<br/>selects entities, fields, and target grain"]
    A -.->|"JoinLint does not establish"| GAP["Business intent and<br/>entity-set completeness"]

    A -->|"inspect schema"| DBMCP["Separate database MCP"]
    DBMCP --> DB["SQLite database"]

    subgraph PRODUCT["JoinLint Stage 1 Developer Preview"]
        MCP["Local read-only STDIO MCP<br/>exactly two tools"]
        PLAN["get_join_plan<br/>returns immutable Join Proof"]
        VALIDATE["validate_sql<br/>parses final SQL and binds proof"]
        RUNTIME["Deterministic runtime<br/>relationship evidence + graph planning"]
        INPUTS["SQLite source + declared FK<br/>+ curated model"]
        PARSER["SQLGlot parse-only validator"]
        CACHE["Private sanitized<br/>content-addressed cache"]
        OUTPUT["Scope + findings + recovery guidance<br/>execution_count = 0"]
        CLI["Separate human CLI<br/>scan → candidates → accept → check"]

        MCP --> PLAN --> RUNTIME
        MCP --> VALIDATE --> PARSER --> RUNTIME
        INPUTS --> RUNTIME
        CLI --> INPUTS
        RUNTIME <--> CACHE
        RUNTIME --> OUTPUT
    end

    A -->|"1. entity refs and grain"| MCP
    OUTPUT -->|"proof or inconclusive"| A
    A -->|"2. final SQL and plan_id"| VALIDATE
    OUTPUT -->|"execute only after pass"| DBMCP
    INPUTS -.->|"read evidence only"| DB

    MCP -.->|"explicitly outside scope"| BOUNDARY["Business semantics, metrics, filters,<br/>answer correctness, relationship discovery,<br/>and complete entity selection"]
```

## Formal experiment and evidence path

```mermaid
flowchart TB
    TASKS["Frozen tasks and data<br/>trusted query contract when applicable"]
    LOCK["Input lock + run plan + lineage<br/>bind commit, samples, hosts, and model"]
    GUARD["GitHub admission gate<br/>authorization, digests, duplicates, environment"]
    BUDGET["Atomic campaign ledger<br/>reserve → consume → settle"]
    INCIDENTS["Failure ledger<br/>Pattern-Key + recurrence guard"]

    RUNNER["Inspect AI harness<br/>GitHub Actions + Modal<br/>fixed Codex / Claude host and model"]
    CONTROL["Control<br/>same task, model, host, database MCP<br/>without JoinLint"]
    TREATMENT["Treatment<br/>only difference: current JoinLint MCP"]
    PRODUCT["Same Stage 1<br/>product implementation"]

    TRACE["Trace and mechanism scorer<br/>plan call / grounding / SQL validation / protocol"]
    ITT["Paired intention-to-treat rows<br/>isolated infrastructure failures remain zeros"]
    STATS["Join-Correct Task Completion<br/>exact two-sided McNemar test"]
    VERIFY["Independent strict verifier<br/>inventory, digests, run identity"]
    PUBLIC["Durable sanitized evidence<br/>result docs + Draft Release + public ledger"]
    RAW["Private raw traces<br/>runner-ephemeral or ignored local preservation"]

    TASKS --> LOCK --> GUARD --> RUNNER
    BUDGET --> GUARD
    INCIDENTS --> GUARD
    RUNNER --> CONTROL --> TRACE
    RUNNER --> TREATMENT --> TRACE
    PRODUCT -.->|"reused, not an evaluation fork"| TREATMENT
    RUNNER -.-> RAW
    TRACE --> ITT --> STATS --> VERIFY --> PUBLIC

    STATS --> POSITIVE["Supported narrow result<br/>opaque-relationship stress:<br/>6/20 → 20/20; p = 0.0001220703125"]
    STATS --> COUNTER["Constraint evidence<br/>semantic-safety negative;<br/>local BIRD negative diagnostic"]
    POSITIVE --> LIMIT["No general natural-language SQL,<br/>business-semantic, discovery,<br/>or entity-selection claim"]
    COUNTER --> LIMIT
```

The experiment treatment reuses the current MCP implementation rather than a
special evaluation-only product. The most important boundary is upstream:
JoinLint can prove relationships among requested physical entities, but it
cannot prove that the Agent selected every entity required by the user's intent.
