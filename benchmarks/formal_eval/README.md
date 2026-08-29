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
python -m benchmarks.formal_eval.cli guidance-smoke \
  --output /tmp/joinlint-guidance-smoke
python -m pytest tests/test_formal_eval_*.py -q
```

The fake-model output is deliberately synthetic and proves only that contracts,
gates, statistics, report generation, and the 2×2 matrix are wired correctly.
It is never product evidence.

`inspect-smoke` additionally exercises the current two-tool MCP through Inspect
AI's mock model and the formal trace scorer. It is a local wiring check, not a
diagnostic canary and not formal Agent evidence.

`guidance-smoke` runs four frozen non-OK response cases with and without MCP v3
recovery guidance. Its scripted mock policy proves only that the ablation,
submission contract, and scorer are wired correctly. Its arm difference is
synthetic and must never be reported as evidence that a real model improves.

## Evaluation lifecycle boundary

Formal Agent tasks use five separate stages:

1. infrastructure preparation verifies the image-installed host CLI, checks
   that JoinLint imports and the uploaded SQLite database opens read-only, and
   forces one remote command so Inspect sandbox-tools are injected and running;
2. Agent bridge and MCP preparation remain infrastructure work and reuse the
   readiness-attested sandbox-tools service;
3. the pinned host context profile removes unneeded shell, file, web, sub-agent,
   scheduling, and workspace tools; the first bridged request must expose the
   exact required MCP tools and no unapproved tools before readiness is attested;
4. evaluation starts only after that tool-surface check passes at the first
   bridged model request;
5. semantic scorers run only after evaluation completes.

Each stage records its own status and duration in the Inspect sample store.
Host-binary and sandbox probes, their bounded retry, Agent bridge preparation,
and MCP startup share one pre-model readiness window. The evaluation window is
separate, while the Modal sandbox timeout remains the global bound across both.
Readiness failures and Agents that never reach a first model request produce a
task outcome with `INFRASTRUCTURE_FAILURE`; they are never interpreted as SQL
parse failures. A timeout after the first model request remains `MODEL_TIMEOUT`.
Formal effect stages retain isolated infrastructure failures in the
intention-to-treat denominator and stop only for an incomplete batch or a
systemic batch failure. Calibration remains fail-closed on any infrastructure
failure because it is an admission gate rather than an effect estimate.
Message, turn, or token exhaustion after that boundary remains `MODEL_LIMIT`.
Tool-surface drift is a readiness failure and stops before the provider model is
called, so a host upgrade cannot silently restore a large coding-agent context.
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
precursor to a paid Agent canary. It starts both pinned hosts with the exact Pilot
Dockerfile and MCP configuration, intercepts each first bridged request before
the provider model, and attests the reduced tool surface with zero provider
tokens. It exposes only Modal credentials and emits a commit-bound readiness
report. Its exact CNY 2.10 approval covers the CNY 0.063456 two-sandbox compute
upper bound plus the existing CNY 2.00 image-build reserve.
Readiness, the one-task canary, and calibration all require the cumulative
campaign ceiling and prior spend; each fails locally before Modal or the model
provider when its full approved per-run ceiling would exceed the remainder.
Paid calibration and the preregistered Flash Pilot stage use an atomic campaign
reservation rather than a manually supplied prior-spend snapshot. Other paid
modes remain fail-closed until they have the same admission boundary and a
frozen resource envelope.
All five paid jobs share the repository-scoped `joinlint-formal-paid-v1`
concurrency group with cancellation disabled, so at most one can run at a time
after they are re-enabled. This single-flight guard is defense in depth only:
it does not reserve funds atomically, and GitHub may replace an older pending
job with a newer pending job.
`campaign_ledger.py` now supplies the tested accounting primitive for the next
gate: strict integer micro-CNY accounting, append-only full-upper-bound
reservations, evidence-bound conservative settlements, and a non-force GitHub
ref compare-and-swap. Schema v2 reservation-only ledgers remain readable; the
first settlement upgrades the append-only event stream to schema v3. A
settlement may only reduce one existing reservation to a still-conservative
accounted upper bound and permanently binds the supporting artifact digest.
Exact reservation replays are
recorded as already consumed and never authorize another paid call. Deterministic
tests cover same-head writers, response loss, replay, and exhausted-budget
conflicts. Existing-branch reads require the expected GitHub repository ID and
an externally pinned empty genesis commit, then verify at most one Git commit
per recorded reservation or settlement plus the genesis. Histories outside
that genesis, merges, skipped or replaced events, and changed budget or
opening-balance fields fail closed; CAS rechecks that lineage before creating
any Git object.
Genesis ancestry alone cannot distinguish the current head from a force-reset
to one of its formerly valid prefixes, so a protected non-force/non-delete ref
or an independent monotonic checkpoint remains mandatory. The completed
semantic-safety campaign remains fixed to its public
`joinlint-campaign-ledger-safety-v1` branch, empty genesis
`96d689731148b8eb69812b4d0a82dcac75bf2fd1`, CNY 60 ceiling, and CNY 46.717684
conservative opening reserved upper balance. The query-contract campaign uses
protected public branch `joinlint-campaign-ledger-contract-v1`, empty genesis
`3c33a92297e4a8b530a80cfbb6cdf6caa0931aef`, CNY 70 ceiling, and CNY 57.606231
conservative opening reserved upper balance. Every newly enabled paid mode still
needs a reviewed admission policy and protected non-force ledger ref.
`campaign_reservation.py` narrows the workflow-owned consumer to GitHub's
default run variables on an explicitly protected ref, fixes the admitted
mode/upper pairs to readiness/CNY 2.10, calibration/CNY 4, full Pilot/CNY 20,
the BIRD Flash full-dataset stage/CNY 7.40, the synthetic semantic-safety Flash
stage/CNY 8.00, and the posthoc synthetic hard-case confirmation/CNY 5.53,
requires the exact matching policy-driving dispatch flags and budget string,
reads the actual Git HEAD from a clean fixed checkout path, and
requires calibration/Pilot reservations to bind the verified frozen-input lock
digest, including every task's consumed database.
`reserve_current_run` returns only for a newly authorized reservation that also
matches the live ledger head; a replay is rejected. Standalone receipt
verification is a repeatable snapshot check, not a consume-once execution token.
There is no longer a caller-defined repository/mode/upper reservation command.
This admission contract is trusted only when a clean workflow-owned job runs it
before evaluated code; it is not a portable signed identity proof. Its `Mapping`
arguments do not authenticate their own source or prove ownership of those
directories. Production wiring reads the runner's default environment and event
payload directly, populates the fixed checkout and frozen-input directories in
a clean workflow-owned job, and runs admission before evaluated code. Paid
workflows remain disabled between explicitly approved runs; only the exact
calibration and Flash-stage modes have the current end-to-end admission wiring.
The paid one-task canary uses a 170-second sandbox envelope: its independently
measured 60-second readiness window and 90-second evaluation window leave 20
seconds for lifecycle handoff and evidence finalization. Pilot v3 targets Claude
Code with the cost-efficient model so the host
bridge dependency is exercised before any full matrix run. Its Inspect accounting
limit uses the native price-weighted expression `(input*0.5)+output:60000`:
input is weighted at the cache-miss CNY 1 rate relative to the CNY 2 output rate,
while cache hits are conservatively treated as misses. Because Inspect checks
that stop line only after a complete response, the monetary envelope separately
allows a final full provider context plus the 4,096-token response cap. Its
504,096 weighted-token accounting ceiling produces a CNY 1.008192 model upper
bound and CNY 3.05314 total, which exceeds the old CNY 2.25 approval. The canary
therefore remains fail-closed pending a new frozen budget or a provider-enforced
pre-response limit. This
canary-only envelope does not change the separately frozen 20-task Pilot
registration. A green result proves only that the selected Claude/Flash path
reached scoring; it does not authorize a full Pilot dispatch.

## Sealed four-cell calibration

The `calibration` mode of `formal-pilot-canary.yml` is the required gate before
another full Pilot.

For the treatment arm, an evaluation-only sidecar records only the SHA-256 of
the most recent `validate_sql` response with `status: ok`. The separate
submission MCP accepts non-empty SQL only when its exact bytes match that
record. This prevents a validated SQL statement from being silently replaced at
submission time; it is not a JoinLint product feature and does not alter the
released two-tool MCP contract.

The sealed input freezes two task IDs from distinct databases using the deterministic
`highest_join_depth_distinct_database_then_task_id_v2` rule. Each task runs in treatment on both
model tiers and both hosts, producing eight samples. Calibration uses the exact
formal contract: a 35,000 native price-weighted runtime stop line and a 45,000
accounting ceiling on each host, plus 20 messages, 90 seconds after the first
model request, and a 170-second Modal sandbox. The formal workflow rejects an
attestation unless these limits match its registration and the frozen budget
envelope remains approved.

Calibration schema v7 separates readiness evidence from the observed product
outcome. Infrastructure attestation requires every model/host/task cell to
prepare the selected host binary. Resource attestation records uncached input,
cache read, cache write, output, Inspect-weighted usage, headroom, and frozen-price
cost for every cell. It retains limit-censored product outcomes, while the schema
v7 authorization check independently requires complete accounting inside the
frozen token/cost envelope. A message, turn, token, or time limit therefore remains
an Agent outcome rather than becoming an infrastructure failure.
Scoring-pipeline readiness requires both scorer artifacts even when the model
never produced a parseable submission. Harness and scoring attestations remain
product observations: they report planning, proof binding, MCP grounding,
protocol compliance and any bounded protocol-violation code, scoring eligibility,
parseability, and the evaluation-only submission guard decision. Guard rejection
remains a product outcome; missing or malformed guard evidence blocks authorization.
An evaluation-side ledger write failure is reported as infrastructure failure and
also blocks authorization rather than increasing the product tool-error rate.
Before a treatment Agent starts, readiness arms a sticky sandbox marker. The
recording MCP flips that marker before returning a ledger-write error, and the
lifecycle reads it at the terminal boundary even when the Agent makes no later
provider request. The marker contains no SQL or exception detail; a missing or
malformed armed marker fails closed as evaluation infrastructure rather than
being scored as a model outcome. Provider-request and final-message checks
remain independent secondary evidence for the same failure.
Schema v7 otherwise authorizes only from infrastructure/resource/scoring-pipeline
readiness and budget status, so a product failure cannot preselect tasks that
already work.
Schema v1-v6 artifacts remain parseable but cannot authorize the current Pilot.

The eight-run ceiling is CNY 4.00: a modeled CNY 1.44 token upper bound, CNY
0.359584 Modal-compute upper bound, and CNY 2.00 image-build reserve. Dispatch
also requires the previously accumulated investigation spend and cumulative
campaign budget; both values are recorded in the attestation. The weighted
token expression remains a conservative prompt-complexity limit, not a
cache-aware monetary meter. Actual model cost is calculated separately from
cache-hit, uncached-input, cache-write, and output usage. Cache reads can be
cheap in CNY while still consuming the Inspect context limit; both facts are
reported rather than collapsed into one token number.

Pilot dataset v4 excludes four BIRD Train tasks whose question and gold SQL
conflict or admit materially different entity sets: `citeseer-04142`,
`citeseer-04143`, `citeseer-04150`, and `trains-00698`. It also drops the
`genes` database entirely because none of its declared-FK tasks have an entity
with a stable unique grain, so JoinLint Stage 1 cannot certify a Join Proof for
them. The frozen allocation is `citeseer` 9 and `trains` 11, selected by the
unchanged deterministic rule after the ground-truth exclusions and the
grain-provable oracle gate; no JoinLint output is used to choose replacements.

## Bounded independent pilot

The first product-effect experiment is the preregistered
`flash_full_dataset_v1` stage. It runs the cost-efficient frozen model over all
20 Pilot tasks in paired control/treatment arms: 40 runs and 20 paired units,
balanced across Codex and Claude Code. The primary outcome is
`join_correct_task_completion`; the frozen primary test is exact two-sided
McNemar at alpha 0.05. The stage writes its preregistration and run plan before
the first model call, and binds them to the GitHub run, campaign reservation,
ledger commit, evaluated commit, and frozen input lock. Its CNY 7.39792 resource
upper is reserved as CNY 7.40. A positive significant result supports only an
exploratory claim for this frozen BIRD Pilot, Flash model, and host allocation;
it is not a confirmatory population estimate.

The completed run over the frozen BIRD Pilot produced 17/20 control successes
and 16/20 treatment successes (absolute change -0.05; treatment-only wins 2,
control-only wins 3; exact two-sided McNemar p=1.0). It therefore does not show
a positive product effect on that sample. The three treatment-only regressions
were model-limit outcomes rather than accepted wrong Join paths. Because this
20-task sample has only three control failures, even a perfect treatment could
produce at most three treatment-only wins (two-sided p=0.25); repeating the
same sample cannot establish significance.

The separate `semantic_join_safety_v1` stage is a preregistered synthetic safety
stress test, not a replacement natural-error-rate claim. It freezes 20
independent tasks across four generated SQLite databases, each with a proven
dangerous same-type or wrong-hierarchy join whose result differs from the gold
query. Control receives the visible schema without relationship declarations;
treatment receives the same input plus JoinLint's relationship proof. The
stage keeps the same paired McNemar outcome and alpha, uses the Flash model,
raises the per-run stop/accounting/message limits to 45,000/50,000/30 to avoid
the BIRD run's model-limit censoring, and reserves a CNY 8.00 upper bound for a
CNY 7.79792 resource envelope. Any result supports only the frozen synthetic
Join-safety stress claim.

The completed 20-pair synthetic safety run produced 17/20 control successes
and 14/20 treatment successes (treatment-only wins 0, control-only wins 3;
exact two-sided McNemar p=0.25). It did not show a positive product effect.
The separately preregistered posthoc hard-case confirmation then ran three
Codex repetitions of four frozen tasks. It produced 3/12 control successes and
1/12 treatment success (treatment-only wins 1, control-only wins 3; absolute
change -0.1667; exact two-sided McNemar p=0.625). Eight of the eleven treatment
failures were `ENTITY_SET_INCOMPLETE`; all 12 treatment samples called the plan
tool, used a usable plan, followed the protocol, and validated their final SQL.
This is evidence that the current agent-facing workflow does not reliably map
the full user request to the complete physical entity set. JoinLint proves the
requested join path, but cannot prove that the agent requested every necessary
entity. Neither run supports a positive product-effect claim. The immutable
run IDs, artifact digests, costs, and scope are recorded in
[`docs/semantic-safety-effect-results.md`](../../docs/semantic-safety-effect-results.md).

The next candidate experiment removes the observed entity-selection confound
without rewriting those results. Both arms receive the same input-locked trusted
query contract: complete required physical entities, requested output fields,
and pre-aggregation row grain. Only treatment receives JoinLint relationship
proofs and SQL validation. Its 20 tasks use four new synthetic databases and
reuse none of the observed safety tasks. Deterministic gates prove that every
contract matches the scored entity set, every gold SQL passes its proof, and
every executable dangerous SQL is blocked. No model outcome exists yet, and
the claim remains limited to Join safety downstream of a trusted semantic
mapping. See
[`docs/superpowers/specs/2026-08-28-query-contract-join-safety-evaluation-design.md`](../../docs/superpowers/specs/2026-08-28-query-contract-join-safety-evaluation-design.md).

`formal-pilot.yml` is a separate, manually approved 20-task BIRD Train pilot.
It runs 80 samples under the frozen `balanced_diagonal_crossover_v1` design.
Every task runs on both DeepSeek V4 tiers and both control/treatment arms. For
each task, Pro and Flash use opposite hosts; the assignment reverses across the
stable task ordering, so every model/host cell receives 10 tasks. This preserves
paired arm comparisons and exercises all four model/host cells without treating
the Pilot as a full-factorial effect study. There are no repetitions. Pilot
tasks and outputs never enter the confirmatory effect estimate.

The full 80-run mode remains blocked. Its workflow contract accepts only the
exact CNY 20 approval. The frozen worst-case
resource envelope is CNY 19.99584: CNY 14.40 for model tokens, CNY 3.59584 for
170-second Modal sandbox lifetimes, and a CNY 2.00 image-build reserve. It also
requires the cumulative investigation budget and the spend observed before the
Pilot; dispatch stops unless that prior spend plus the frozen CNY 19.99584 cost
envelope fits inside the campaign budget. The CNY 20 per-run hard stop remains
unchanged, so unused approval headroom is not counted as spend twice. Each
sample has the native price-weighted token limit exercised by the required
four-cell calibration, `(input*0.5)+output:35000`, with a separate 45,000
accounting ceiling. Inspect checks the runtime stop line after a model response;
the 10,000-token accounting reserve is nearly twice the largest 5,123-token
single-response increment observed in the sealed calibration. Each sample has a
60-second readiness limit and a 90-second evaluation limit that starts at the
first model request; each Modal sandbox has a platform-level 170-second
automatic timeout, leaving a 20-second lifecycle-finalization margin. A
20-message limit bounds
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
Partial sanitized budget checkpoints are retained if a batch stops. Raw Inspect
logs remain only on the ephemeral Actions runner and are never uploaded. This
code-side envelope does not replace provider billing alerts or account spend limits.

Formal evidentiary and paid sanitized uploads run a strict public-artifact
verifier from a separate checkout pinned to a reviewed commit, never from the
evaluated or frozen Pilot checkout. Each profile requires its exact terminal
inventory or documented ordered failure prefix, strict typed schemas,
duplicate-free finite RFC 8785 JSON, and deterministic JSON-to-Markdown and
cross-file consistency where applicable. Unknown paths, symbolic links,
special files, raw private fields, SQL-shaped text, and credential-shaped text
fail closed. The lightweight deterministic smoke artifact is not evidentiary
and remains outside this stronger contract. Content validation does not prove
source-run identity or reserve campaign funds; those remain separate gates.
The separate checkout pins validator code provenance but shares the job runner;
it is not an adversarial process sandbox. Paid and report jobs therefore stay
hard-blocked until the evaluated commit is an approved trust input or content
validation moves to a clean verifier runner, in addition to their other gates.
Every `uses:` dependency in the repository workflows, including CI and the
hard-blocked paid/report jobs, is fixed to an approved full commit SHA
corresponding to its adjacent official release version. The version comments
are informational, and the workflow pinning test rejects unapproved remote,
local, Docker, tag, branch, or revision references.

## Formal remote run

Formal inputs stay under the ignored `sealed/` boundary described in
[`sealed/README.md`](sealed/README.md). When the paid path is re-enabled, use
the protected `formal-evaluation` GitHub Actions workflow; never launch that
batch from a developer shell.

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

The legacy paid `formal-evaluation` job is fail-closed until it has both an
atomic campaign reservation and an access-controlled frozen-input store. Do not
upload the ignored sealed directory as an Actions artifact: in a public
repository that artifact is not a private evidence boundary. The deterministic
job remains available without provider credentials or paid dispatch.

After the batch, independently review the exact sample IDs frozen in the run
plan without arm labels. Store per-sample `BlindReviewDecision` records, output
digests, reviewer-ID digests, and the arm-blinding attestation in a private
`joinlint-formal-blind-review` artifact.
The `formal-evaluation-report.yml` workflow is currently fail-closed until a
workflow-owned source-run verifier validates the source workflow, repository,
commit, conclusion, and first-attempt metadata for the formal, frozen-input,
and review artifact runs before downloading private evidence. Once re-enabled,
it revalidates the original input lock and rebuilds the only report eligible
for a public improvement claim. This post-run step prevents review agreement
from being frozen before the outputs being reviewed exist; reviewer identity
and blinding remain part of the controlled review procedure.

Provider credentials are scoped only to the two Modal dispatch steps. The
runtime contract check and deterministic JoinLint MCP subprocesses receive a
small allowlisted environment without model, GitHub, or Modal credentials.

## BIRD subset preparation

The naturalistic study needs at least 12 databases, while the official BIRD
Dev release contains 11. The protected `prepare-bird-subset` paid job is
currently fail-closed pending an atomic campaign reservation and an
access-controlled destination for its sealed output. When re-enabled, it
obtains additional Train databases without making a developer machine a runner:

1. GitHub Actions requires the `formal-evaluation` Environment approval and an
   explicit `confirm_paid` input.
2. Modal downloads the frozen official Train archive into 25 GB of ephemeral
   disk. The source size, ETag, nested-member size, and nested-member CRC are
   checked before use; a complete SHA-256 is recorded after download.
3. Qualification uses only frozen BIRD SQL structure: one to four supported
   equality joins, deterministic SQLGlot parsing, at least five eligible tasks
   per selected database, and a physical grain-preserving join path derived
   from the frozen database's declared foreign keys and unique keys. A task
   whose expected grain entity has no stable unique key, or whose only path
   fans out from every unique-key entity, is ineligible even when its gold SQL
   executes, because JoinLint Stage 1 cannot certify a Join Proof for it.
   Qualification never reads JoinLint output.
4. The remote Volume receives only selected SQLite files, matching task/schema
   metadata, and `source-manifest.json`. GitHub rechecks every hash and SQLite
   file before handing the subset to the access-controlled destination, then
   removes the temporary remote subset. It must never upload sealed output as a
   public-repository Actions artifact.

The default Train IDs are acquisition candidates, not a frozen pilot or
confirmatory allocation. Review allowed join graphs and complete the independent
pilot before copying any candidate into the sealed formal manifest. Starting
this workflow creates Modal compute, transfer, and temporary storage charges;
the workflow is not called by ordinary CI or by the formal-evaluation workflow.

## Evidence boundary

- Natural and adversarial relationship corpora are never pooled.
- BIRD confirmatory tasks and semantic-join-failure diagnostics are never
  pooled into one error rate.
- Raw questions, schema text, gold SQL, database paths, and review state stay in
  the access-controlled frozen-input boundary.
- Raw Inspect transcripts remain runner-ephemeral; public Actions never upload
  them.
- Formal evidentiary and paid sanitized artifacts pass the pinned strict
  public-artifact verifier and contain identifiers, hashes, counts, rates,
  intervals, failure categories, model and host versions, commit and image
  identity, lineage and input digests, latency, memory, tokens, and cost only.
- Join correctness is never described as proof of metric meaning or complete
  answer correctness.
