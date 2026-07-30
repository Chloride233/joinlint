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
   `GRAIN_INCOMPATIBLE`, `COMPOUND_FANOUT`, `NO_VERIFIED_PATH`, or another
   non-OK status, do not retry equivalent ref sets or guess a join. Ask for
   clarification or report the stable blocker.
4. Generate SQL using only the returned proof edges. Preserve the returned
   composite predicates as one relationship.
5. Call `validate_sql` with the exact final SQL and returned `plan_id`.
6. If validation is blocking, inconclusive, stale, unavailable, or errors, do
   not execute the SQL. Replan or explain the blocker.
7. Execute only through the separately configured database tool after
   validation passes. Never treat a JoinLint pass as proof that filters,
   aggregations, metrics, business meaning, or the answer are correct.

**JoinLint proof != query correctness.**
