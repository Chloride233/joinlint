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
3. Plan once after choosing the complete entity set. If planning returns
   a non-OK status, follow `error.guidance.next_action`. `retryable` means retry
   only after applying that action; never retry an unchanged request. If the
   action is `stop`, do not guess a join or execute SQL.
4. Generate SQL using only the returned proof edges. Preserve the returned
   composite predicates as one relationship.
5. Call `validate_sql` with the exact final SQL and returned `plan_id`.
6. If validation is blocking, inconclusive, stale, unavailable, or errors, do
   not execute the SQL. Follow the returned finding/error guidance exactly;
   `stop` is terminal for that operation.
7. Execute only through the separately configured database tool after
   validation passes. Never treat a JoinLint pass as proof that filters,
   aggregations, metrics, business meaning, or the answer are correct.

**JoinLint proof != query correctness.**
