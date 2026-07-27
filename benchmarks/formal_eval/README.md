# Formal Evaluation v2

This directory implements JoinLint's completed-product evaluation contract. It
keeps three questions separate:

1. whether relationship evidence is safe to use automatically;
2. whether SQL validation blocks dangerous joins without blocking supported
   safe SQL;
3. whether the complete MCP increases join-correct Agent task completion.

The primary outcome is **Join-Correct Task Completion Rate**. Missing SQL,
refusal when an oracle path exists, timeout, malformed output, missing joins,
extra joins, and validation bypass remain failures. A refusal is successful
only when the frozen oracle also has no safe path.

## Free deterministic smoke

After installing `.[dev,eval]`, run:

```bash
python -m benchmarks.formal_eval.cli fake-model \
  --output /tmp/joinlint-formal-smoke
python -m benchmarks.formal_eval.cli inspect-smoke \
  --project tests/fixtures/chinook \
  --output /tmp/joinlint-inspect-smoke
python -m pytest tests/test_formal_eval_*.py -q
```

The fake-model output is deliberately synthetic and proves only that contracts,
gates, statistics, report generation, and the 2×2 matrix are wired correctly.
It is never product evidence.

`inspect-smoke` additionally exercises the current two-tool MCP through Inspect
AI's mock model and the formal trace scorer. It is a local wiring check, not a
diagnostic canary and not formal Agent evidence.

## Evaluation lifecycle boundary

Formal Agent tasks use four separate stages:

1. infrastructure preparation installs and verifies the pinned host CLI and
   checks that JoinLint imports and the uploaded SQLite database opens read-only;
2. Agent bridge and MCP preparation remain infrastructure work;
3. readiness is attested and evaluation starts only at the first bridged model
   request;
4. semantic scorers run only after evaluation completes.

Each stage records its own status and duration in the Inspect sample store.
Readiness failures and Agents that never reach a first model request produce a
task outcome with `INFRASTRUCTURE_FAILURE`; they are never interpreted as SQL
parse failures. A timeout after the first model request remains `MODEL_TIMEOUT`.
The no-model lifecycle test is a mandatory workflow gate, and a Pilot canary
cannot produce an attestation unless its semantic scorers report a completed,
scoring-eligible evaluation.

## Bounded independent pilot

`formal-pilot.yml` is a separate, manually approved 20-task BIRD Train pilot.
It runs 160 samples: 20 tasks × 2 DeepSeek V4 tiers × 2 hosts × control/treatment,
with no repetitions. Pilot tasks and outputs never enter the confirmatory
effect estimate.

The workflow accepts only the exact CNY 20 approval. The frozen worst-case
resource envelope is CNY 19.87648: CNY 12.80 for model tokens, CNY 5.07648 for
120-second Modal sandbox lifetimes, and a CNY 2.00 image-build reserve. Each
sample has a 20,000-token limit, a 30-second readiness limit, and a 90-second
evaluation limit that starts at the first model request; each Modal sandbox has
a platform-level 120-second automatic timeout. Model retries are disabled,
concurrency is two, and the dispatcher stops before the next 20-task batch when
observed cost plus every remaining worst-case batch would exceed CNY 20.

The frozen inputs are packaged as one access-controlled GitHub Draft Release
asset. The manual workflow checks out the full commit bound to that asset,
which remains correct even if the PR itself enters the default branch through
a merge or squash commit. After download and extraction, the workflow checks
the complete content lock before proceeding. This avoids requiring a developer
machine to connect to Modal. Because GitHub limits Draft Releases to maintainers,
the protected manual workflow receives a repository `contents: write` token
solely to read that input asset; it does not invoke a GitHub write operation.
Only the paid dispatch step receives both Modal and DeepSeek credentials.
Partial budget checkpoints and private logs are retained if a batch stops. This
code-side envelope does not replace provider billing alerts or account spend
limits.

## Formal remote run

Formal inputs stay under the ignored `sealed/` boundary described in
[`sealed/README.md`](sealed/README.md). Use the `formal-evaluation` GitHub
Actions workflow; do not launch a paid batch from a developer shell.

The protected workflow:

1. runs all deterministic tests and the fake pipeline;
2. verifies the input lock, content-derived near-duplicate fingerprints, split
   isolation, sample floor, returned model identities, host versions, policy version,
   commit, and image digest, then creates one evaluation lineage;
3. requires the Stage 1 runtime to expose exactly `get_join_plan` and
   `validate_sql`, and rejects the hidden historical-pilot server;
4. derives a database-level power requirement from the independent pilot and
   stops before dispatch unless the frozen manifest and exact run plan meet it;
5. generates relationship, SQL, and performance evidence from the locked raw
   suite and binds it to both the suite digest and evaluation lineage;
6. uses Inspect AI and Inspect SWE to run pinned Codex CLI and Claude Code hosts
   in fresh Modal sandboxes from the preregistered digest-pinned image;
7. exports only the exact run-plan sample IDs and always writes the full report,
   including null or harmful effects. This first report is deliberately ineligible for a public
   improvement claim until post-run blinded review is attached.

Configure a protected GitHub Environment named `formal-evaluation` with Modal
credentials and only the provider keys required by the two frozen model IDs.
Upload the ignored sealed directory as the private `joinlint-formal-inputs`
artifact in a controlled freeze run; dispatch the formal workflow with that
run ID. The workflow downloads it only after environment approval and verifies
every required file against the input lock. It still requires the explicit
`confirm_paid` input.

After the batch, independently review the exact sample IDs frozen in the run
plan without arm labels. Store per-sample `BlindReviewDecision` records, output
digests, reviewer-ID digests, and the arm-blinding attestation in a private
`joinlint-formal-blind-review` artifact.
Run `formal-evaluation-report.yml` with the original formal, frozen-input, and
review artifact run IDs. That workflow checks out the preregistered commit,
revalidates the original input lock, and rebuilds the only report eligible for
a public improvement claim. This post-run step prevents review agreement from
being frozen before the outputs being reviewed exist; reviewer identity and
blinding remain part of the controlled review procedure.

Provider credentials are scoped only to the two Modal dispatch steps. The
runtime contract check and deterministic JoinLint MCP subprocesses receive a
small allowlisted environment without model, GitHub, or Modal credentials.

## BIRD subset preparation

The naturalistic study needs at least 12 databases, while the official BIRD
Dev release contains 11. The protected `prepare-bird-subset` workflow obtains
additional Train databases without making a developer machine a runner:

1. GitHub Actions requires the `formal-evaluation` Environment approval and an
   explicit `confirm_paid` input.
2. Modal downloads the frozen official Train archive into 25 GB of ephemeral
   disk. The source size, ETag, nested-member size, and nested-member CRC are
   checked before use; a complete SHA-256 is recorded after download.
3. Qualification uses only frozen BIRD SQL structure: one to four supported
   equality joins, deterministic SQLGlot parsing, and at least five eligible
   tasks per selected database. It never reads JoinLint output.
4. The remote Volume receives only selected SQLite files, matching task/schema
   metadata, and `source-manifest.json`. GitHub rechecks every hash and SQLite
   file before creating the access-controlled seven-day artifact, then removes the
   temporary remote subset.

The default Train IDs are acquisition candidates, not a frozen pilot or
confirmatory allocation. Review allowed join graphs and complete the independent
pilot before copying any candidate into the sealed formal manifest. Starting
this workflow creates Modal compute, transfer, and temporary storage charges;
the workflow is not called by ordinary CI or by the formal-evaluation workflow.

## Evidence boundary

- Natural and adversarial relationship corpora are never pooled.
- BIRD confirmatory tasks and semantic-join-failure diagnostics are never
  pooled into one error rate.
- Raw questions, schema text, gold SQL, database paths, review state, and
  Inspect transcripts remain private.
- Public artifacts contain identifiers, hashes, counts, rates, intervals,
  failure categories, model and host versions, commit and image identity,
  lineage and input digests, latency, memory, tokens, and cost only.
- Join correctness is never described as proof of metric meaning or complete
  answer correctness.
