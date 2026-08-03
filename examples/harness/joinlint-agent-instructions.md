# JoinLint SQL Harness

For every multi-table SQLite task:

1. Use the separately configured database tool to inspect schema. Do not ask
   JoinLint for schema or data.
2. Call `get_join_plan` with every intended table instance. Give each instance
   a unique request-local `ref`, including separate refs for self joins. Set
   `start_ref` and `expected_grain_ref` to the intended row grain. The expected
   grain is the instance whose unique key must remain one row per output row
   before aggregation. For many-to-one joins, this is normally the referencing
   child; aggregation, `DISTINCT`, and `GROUP BY` do not restore grain in Stage 1.
3. Plan once after choosing the complete entity set. A retryable
   `UNCONNECTED_ENTITY_REF` permits one changed request that removes or replaces
   only affected refs. A retryable planning `GRAIN_INCOMPATIBLE` permits one
   changed request that changes only `expected_grain_ref`. Do not make another
   planning call for any other result or retry an unchanged request.
4. Generate SQL using only the returned proof edges. Preserve the returned
   composite predicates as one relationship.
5. Call `validate_sql` with the exact final SQL and returned `plan_id`. Omit
   `expected_grain_ref`; the proof already binds it.
6. A non-OK validation blocks the current SQL. Only a retryable `revise_sql`
   action permits one changed SQL-only revision and one more validation with
   the same `plan_id`. Do not plan after validation or change the grain during
   validation. Every other non-OK result is terminal for the operation.
7. Execute only through the separately configured database tool after
   validation passes. Never treat a JoinLint pass as proof that filters,
   aggregations, metrics, business meaning, or the answer are correct.

**JoinLint proof != query correctness.**
