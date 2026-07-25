# Vendored Spider equivalence functions

`spider_result_eq.py` copies only `permute_tuple`, `unorder_row`, `quick_rej`,
`multiset_eq`, `get_constraint_permutation`, and `result_eq` from
`exec_eval.py` in the official `taoyds/test-suite-sql-eval` repository at
commit `e97acc546ecbee8fa27fa8dbf025ef61493a876c`:

<https://github.com/taoyds/test-suite-sql-eval/blob/e97acc546ecbee8fa27fa8dbf025ef61493a876c/exec_eval.py>

The upstream repository is licensed under the Apache License 2.0:

<https://github.com/taoyds/test-suite-sql-eval/blob/e97acc546ecbee8fa27fa8dbf025ef61493a876c/LICENSE>

The copied function bodies are unchanged. JoinLint supplies a separate,
bounded, read-only SQLite executor and only passes materialized result rows to
the upstream `result_eq` rule.
