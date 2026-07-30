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

1. infrastructure preparation verifies the image-installed host CLI, checks
   that JoinLint imports and the uploaded SQLite database opens read-only, and
   forces one remote command so Inspect sandbox-tools are injected and running;
2. Agent bridge and MCP preparation remain infrastructure work and reuse the
   readiness-attested sandbox-tools service;
3. readiness is attested and evaluation starts only at the first bridged model
   request;
4. semantic scorers run only after evaluation completes.

Each stage records its own status and duration in the Inspect sample store.
Readiness failures and Agents that never reach a first model request produce a
task outcome with `INFRASTRUCTURE_FAILURE`; they are never interpreted as SQL
parse failures. A timeout after the first model request remains `MODEL_TIMEOUT`.
Message, turn, or token exhaustion after that boundary remains `MODEL_LIMIT`.
The no-model lifecycle test is a mandatory workflow gate. The legacy one-cell
Pilot canary is operational evidence only; it is not accepted as evidence that
the full Pilot matrix or its resource contract is ready.

Formal images download and checksum the exact frozen Codex and Claude host
binaries in a stable layer before copying frequently changing JoinLint source.
Modal can therefore reuse that layer across JoinLint commits. Each sample only
verifies the image-installed version and SHA-256; it does not download or
transfer host binaries.

`inspect-sandboxes==0.4.0` still calls Modal's deprecated `Sandbox.open` and
`Sandbox.mkdir` APIs. The formal runtime installs a version-locked compatibility
shim that uses Modal's current `Sandbox.filesystem` API for file transfer. The
shim fails closed when the pinned adapter version changes so an upstream upgrade
must be reviewed rather than silently inheriting the patch.

The `readiness_only` mode of `formal-pilot-canary.yml` is the manual, no-model
precursor to a paid Agent canary. It uses the exact Pilot Dockerfile and lifecycle
limits, exposes only Modal credentials, rejects any model usage, and emits a
commit-bound readiness report. Its exact CNY 2.05 approval covers the CNY
0.031728 sandbox-compute upper bound plus the existing CNY 2.00 image-build reserve.
The paid one-task canary uses a 150-second sandbox envelope so its independently
measured 60-second readiness window does not consume the 90-second evaluation
window. Pilot v3 targets Claude Code with the cost-efficient model so the host
bridge dependency is exercised before any full matrix run. Its Inspect accounting
limit uses the native price-weighted expression `(input*0.5)+output:60000`:
input is weighted at the cache-miss CNY 1 rate relative to the CNY 2 output rate,
while cache hits are conservatively treated as misses. The resulting CNY 0.12
model ceiling keeps the complete canary under its CNY 2.25 gate. This
canary-only envelope does not change the separately frozen 20-task Pilot
registration. A green result proves only that the selected Claude/Flash path
reached scoring; it does not authorize a full Pilot dispatch.

## Sealed four-cell calibration

The `calibration` mode of `formal-pilot-canary.yml` is the required gate before
another full Pilot.
The sealed input freezes two task IDs using the deterministic
`highest_join_depth_then_task_id_v1` rule. Each task runs in treatment on both
model tiers and both hosts, producing eight samples. Unlike the legacy canary,
calibration freezes the observed host-aware candidate limits: 35,000 native
price-weighted tokens for Codex and 60,000 for Claude Code, plus 20 messages,
90 seconds after the first model request, and a 150-second Modal sandbox. A
passing candidate calibration cannot authorize the full Pilot until the formal
registration uses the same limits and its budget envelope is approved.

Calibration schema v2 separates readiness evidence from the observed product
outcome. Infrastructure attestation requires every model/host/task cell to
prepare the selected host binary. Resource attestation records uncached input,
cache read, cache write, output, Inspect-weighted usage, headroom, and frozen-price
cost for every cell; it fails on a model/time limit or insufficient headroom.
Scoring-pipeline readiness requires both scorer artifacts even when the model
never produced a parseable submission. Harness and scoring attestations remain
product observations: they report planning, proof binding, MCP grounding,
scoring eligibility, and parseability, but cannot make an under-resourced run
look ready or preselect tasks that already work. The legacy overall `status`
remains fail-closed for the current Pilot gate, while `readiness_status` exposes
the independent infrastructure/resource/scoring-pipeline result. Schema v1
artifacts remain parseable and do not gain a retroactive readiness claim.

The eight-run ceiling is CNY 4.00: a modeled CNY 1.52 token upper bound, CNY
0.31728 Modal-compute upper bound, and CNY 2.00 image-build reserve. Dispatch
also requires the previously accumulated investigation spend and cumulative
campaign budget; both values are recorded in the attestation. The weighted
token expression remains a conservative prompt-complexity limit, not a
cache-aware monetary meter. Actual model cost is calculated separately from
cache-hit, uncached-input, cache-write, and output usage. Cache reads can be
cheap in CNY while still consuming the Inspect context limit; both facts are
reported rather than collapsed into one token number.

## Bounded independent pilot

`formal-pilot.yml` is a separate, manually approved 20-task BIRD Train pilot.
It runs 80 samples under the frozen `balanced_diagonal_crossover_v1` design.
Every task runs on both DeepSeek V4 tiers and both control/treatment arms. For
each task, Pro and Flash use opposite hosts; the assignment reverses across the
stable task ordering, so every model/host cell receives 10 tasks. This preserves
paired arm comparisons and exercises all four model/host cells without treating
the Pilot as a full-factorial effect study. There are no repetitions. Pilot
tasks and outputs never enter the confirmatory effect estimate.

The workflow accepts only the exact CNY 20 approval. The frozen worst-case
resource envelope is CNY 16.3728: CNY 11.20 for model tokens, CNY 3.1728 for
150-second Modal sandbox lifetimes, and a CNY 2.00 image-build reserve. It also
requires the cumulative investigation budget and the spend observed before the
Pilot; dispatch stops unless that prior spend plus the full CNY 20 run ceiling
fits inside the campaign budget. Each
sample has the native price-weighted token limit exercised by the required
four-cell calibration, `(input*0.5)+output:35000`, a 60-second readiness limit,
and a 90-second
evaluation limit that starts at the first model request; each Modal sandbox has
a platform-level 150-second automatic timeout. A 20-message limit bounds
runaway host exploration before the token ceiling. The existing 0.5 CPU and 2 GiB
memory allocation remains unchanged rather than introducing an unmeasured
memory reduction. The workspace Image Builder is
operator-confirmed and frozen as `2025.06 Stable`; this declared setting is
bound into the input lock but is not presented as a machine-queried attestation.
Model retries are disabled,
concurrency is two, and the dispatcher stops before the next 10-task batch when
observed cost plus every remaining worst-case batch would exceed CNY 20. Any
sample-level infrastructure lifecycle failure stops the batch; model time or
message/token limits remain explicit Agent outcomes and are not relabeled as
infrastructure failures.

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
