# Agent Join Evaluation

This directory contains an exploratory downstream evaluation of whether an SQL
Agent retrieves and applies JoinLint MCP relationship context. It is separate
from JoinSafetyBench and is not a marketing benchmark.

## Frozen pilot

The Spider pilot contains 16 tasks from four databases. Each task is run in
four arms with three repetitions, producing 192 primary runs. Four
zero-configuration MCP diagnostics and four safety diagnostics bring the paid
batch to exactly 200 runs.

- A: FK-ablated schema, no MCP.
- B: the same schema plus the database-level oracle graph inline.
- C: the same oracle graph delivered through JoinLint MCP.
- D: the blind-reviewed JoinLint graph delivered through MCP.

The official Spider archive is acquired from:

<https://drive.google.com/file/d/1403EGqzIDoHMdQF4c9Bkyl7dZLZ5Wt6J/view?usp=sharing>

The execution equivalence functions are pinned to upstream commit
`e97acc546ecbee8fa27fa8dbf025ef61493a876c` from:

<https://github.com/taoyds/test-suite-sql-eval/blob/e97acc546ecbee8fa27fa8dbf025ef61493a876c/exec_eval.py>

## Credential and artifact boundary

Acquired data, review sheets, raw Inspect logs, and raw exports are ignored by
Git. The DeepSeek credential is read only from `DEEPSEEK_API_KEY`; the public
endpoint is pinned by `DEEPSEEK_BASE_URL=https://api.deepseek.com`. Never put a
credential in this directory, an `.env` file, a command argument, or a report.

No command requiring `DEEPSEEK_API_KEY` may run until the local selector,
scorers, arm-isolation checks, fake-model dry run, relationship review, frozen
hash verification, secret scan, and full test suite pass and the user gives
separate approval for the paid batch.

## Free preparation workflow

Save the archive from the official URL above as `/tmp/spider.zip`, then run:

```bash
python -m benchmarks.agent_join.prepare_spider \
  --archive /tmp/spider.zip \
  --work-dir benchmarks/agent_join/.work \
  --manifest benchmarks/agent_join/tasks/spider-pilot-manifest.json \
  --hashes benchmarks/agent_join/tasks/spider-pilot-hashes.json
python -m benchmarks.agent_join.projects prepare \
  --work-dir benchmarks/agent_join/.work \
  --hashes benchmarks/agent_join/tasks/spider-pilot-hashes.json \
  --oracle-models benchmarks/agent_join/models/oracle \
  --review-dir benchmarks/agent_join/review
```

Complete every pending entity and relationship decision in the ignored review
directory without consulting sealed tasks or oracle models. After the required
independent process audit creates `review/audit.txt`, freeze the reviewed
models:

```bash
python -m benchmarks.agent_join.projects apply \
  --work-dir benchmarks/agent_join/.work \
  --review-dir benchmarks/agent_join/review \
  --joinlint-models benchmarks/agent_join/models/joinlint \
  --hashes benchmarks/agent_join/tasks/spider-pilot-hashes.json
python -m benchmarks.agent_join.joinlint_eval dry-run \
  --work-dir benchmarks/agent_join/.work \
  --log-dir benchmarks/agent_join/logs/dry-run
```

The tracked manifests contain hashes and relative identifiers, never questions
or SQL. Review sheets, extracted Spider data, local MCP projects, and raw logs
remain ignored.
