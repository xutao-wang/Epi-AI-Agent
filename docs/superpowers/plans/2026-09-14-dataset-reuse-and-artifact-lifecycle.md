# Dataset reuse and artifact lifecycle implementation proposal

Date: 2026-09-14

Status: IMPLEMENTATION APPROVED — combined A+B in the feature worktree only.

The user explicitly requested implementation, worktree creation, and testing with
the original cancelled-thread follow-up. This supersedes the documentation-only
gates below. Deployment into the frozen evaluation system remains unapproved.
Recovery must use exact structured state/lineage; no heuristic or lexical query
matching may select the recovery path.

The user agreed that A+B should be one focused reuse/fresh-retry fix using existing
records, with broader cleanup deferred. Explicit approval to implement the updated
detailed proposal has not yet been given. No application code, tests, runtime data, or frontend assets
are to be modified during this phase. After approval, implementation must take
place in a separate worktree.

## 0. Evaluation freeze: controlling scope and release

This revision supersedes the earlier split of A and B into separately approved
changes. A+B is one focused fix; broader cleanup is deferred. The running system
remains an evaluation baseline. Keep it frozen while reviewing the design; the
discussion does not authorize implementation or deployment.

### Responsibilities agreed in the discussion

**Tools create versioned artifacts. Review records decisions about exact artifacts
or actions. The runtime tracks execution and saving progress.**

- Review decisions identify the artifact/action and version. Do not carry approval
  from a previous version or infer it from `committed` or a generic `active` label.
- Cancelling a task does not make its inputs or already-written bytes invalid.
  Preserve existing cancellation records; do not migrate them during the freeze.
- Kind and format are independent: a PNG figure is an artifact kind plus a format;
  CSV export can be another representation of an existing dataset. Do not create
  more lifecycle labels based on filename extensions.
- One analysis-review decision may cover its linked figures/tables. Do not add
  per-output reviews or change publication visibility in this correction.
- Separate responsibilities within the existing code, not three new services,
  a new event-sourcing framework, or another persistent state machine.
- Keep coordinated writes for coupled decisions. Separation does not permit a
  figure and its analysis run to disagree about the same review outcome.

### One complete fix; broader cleanup deferred

| Work | Scope | Boundary |
| --- | --- | --- |
| A+B: reuse or fresh retry | Narrow storage comparison; compatible pending/active reuse; bounded fresh extraction with durable identity; expected stale replacement handling; truthful tool results | One implementation approval and one complete release candidate; incremental tests/review within that scope |
| Broader artifact cleanup (previously C) | Analysis-load error policy, removing duplicate status projections, changing shared schemas | Audit recommendations only; no implementation in this fix; revisit only if evidence justifies it |

A and B name internal steps, not two features. A decides whether reuse is valid;
B starts a fresh retry when it is not. A recoverable error alone is not completion
of the requested behavior. Do not ship or report an A-only result as the full fix.

Fresh retry means a new dataset attempt with its own identity and review outcome,
while preserving the old artifact and any still-valid plan approval. Resuming an
interrupted fresh retry must use the same reserved identity. It must not repeat
creation, overwrite the old dataset, or inherit the old dataset's approval.

Establish those boundaries with existing artifact records, status values, and
persistence journals. Do not add a general artifact framework or another stored
status vocabulary. The reuse/restart decision and its tests should make later
cleanup easier, without requiring that cleanup now.

Keep immutable integrity, authorization, and exact-version review checks. A
previously accepted pending replay must behave as before. An incompatible old
artifact must not be returned as valid. Expected old-result failures may choose
fresh extraction, while failures of the current source or storage service retain
their actionable diagnostics instead of causing recursive retries.

### Approval boundaries

1. Now: update and review documentation only.
2. Next: explicit approval to implement this combined A+B proposal permits work in a
   separate worktree and isolated verification storage.
3. Later: explicit adoption decision after reviewing test/review evidence permits
   delivery. Implementation approval alone does not modify the frozen evaluator.

Do not create worktrees, modify test files, run new evaluation jobs, edit runtime
records, or deploy code during the present documentation phase.

## 1. Target outcome after A and B

An earlier dataset should help a new request, not prevent it from running:

1. Reuse an existing matching dataset when its saved data is valid and the current
   workflow can use it.
2. Automatically start a fresh extraction when the previous result cannot be
   reused, preserving the original dataset and its history.
3. Do not classify ordinary approval, cancellation, supersession, or stale review
   references as data corruption or an internal software error.
4. Keep existing human-review requirements. Do not add checkpoint-skipping behavior.

“Fresh” means a new dataset operation in the same conversation. Keep the user's
request and a still-valid approved plan. Do not reset the conversation or ask the
model to reconstruct everything from scratch.

The original incident ended an agent turn with a terminal tool error. The evidence
does not show that the FastAPI process crashed: its state endpoint returned 200.
This proposal addresses those unexpected turn terminations and uncaught artifact
lookup failures; it does not claim to prevent every possible server crash.

## 2. Findings from the current source

| Location | Finding | Consequence | Proposed treatment |
| --- | --- | --- | --- |
| `epi_agent/db_rag/tools.py`, `_committed_attempt_result` | Compares the entire current dataset with the original saved snapshot and requires `pending_review` | Approval/cancellation changes trigger fatal identity collision | Compare storage content separately; use current status only to choose reuse or fresh extraction |
| `epi_agent/artifacts.py`, `commit_dataset_persistence_attempt` | Committed replay repeats the full-content comparison | Fixing only the tool leaves another failure path | Use the same storage-content comparison in both places |
| `epi_agent/db_rag/tools.py`, `_validate_attempt_replacement_control` | Mutable predecessor and feedback statuses participate in durable-collision checks | Normal lifecycle changes look like corrupted storage | Separate immutable lineage checks from whether a replacement can still proceed |
| `epi_agent/db_rag/tools.py`, `_committed_attempt_result` | Replacement predecessor lookup is not guarded against stale references | Missing predecessor can become terminal `INTERNAL_TOOL_ERROR` | Handle unavailable predecessor explicitly during old-result reconciliation |
| `epi_agent/db_rag/tools.py`, `_dataset_result` | Tool text always reports `status=pending_review` | Reusing an active result would mislead the model | Report the actual current status and appropriate next action |
| `epi_agent/runtime.py`, dataset completion checks | Already distinguishes active datasets from pending review | Existing routing can support active reuse | Preserve that routing and test the returned references |
| `epi_agent/datasets.py`, `resolve_dataset` | Stale references/statuses are recoverable, but file-load failure is fatal | A missing prior file terminates analysis instead of allowing recovery | C: separately consider recoverable expected load failures; no policy change during this fix |
| `epi_agent/tool_packs/analysis/review.py` | Stale run/dataset/output references and status-transition failures already return recoverable errors | Useful existing pattern | Preserve; add regression coverage rather than rewrite |
| `utils/artifact_publication.py` | Native analysis output checks both outer and nested status | Drift can hide output; this is not evidence of a crash | Keep publication protection; audit writer consistency and test atomic updates |
| `epi_agent/runtime.py`, tool execution | Fatal tool errors and unexpected exceptions deliberately terminate a turn | Amplifies bad classification at the tool boundary | Fix classifications at the source, not a global catch-and-continue rule |

The stored incident had only two changed top-level dataset fields: `status` and
`provenance`. The only changed provenance field was `status_transitions`.

## 3. Simplify responsibilities, not just rename labels

The current shared artifact vocabulary is `draft`, `pending_review`, `approved`,
`active`, `superseded`, `cancelled`, and `rejected`. Persistence independently uses
`begun`, `staged`, `promoted`, and `committed`.

These are not eleven mandatory steps for each dataset. Different artifact types
use different subsets, and persistence describes file-writing progress.

### Responsibility boundaries, preserving current serialized labels

| Concern | Authoritative information | What it must not decide |
| --- | --- | --- |
| Saved data identity and integrity | Dataset ID/version, extraction lineage, paths, schema and manifest | Whether the user has approved the result |
| Current review/lifecycle decision | Current artifact status and recorded decisions | Whether unchanged saved bytes are corrupted |
| Current selection | Existing `active_dataset_id` reference | Whether every other saved dataset has ceased to exist or must be rewritten |
| Interrupted write recovery | Existing persistence attempt/journal | Whether a later review decision invalidates a successful write |

Do not add a new stored `reusable`, `retryable`, `stale`, `restarted`, or
`completed` status. Reuse versus fresh extraction is a decision made when handling
a request, not another artifact lifecycle to maintain.

Keep the existing serialized labels for compatibility with history, exports,
review controls, and old checkpoints. Reduce their influence: only review/use
rules consult review status; only storage validation checks integrity.

For native file artifacts, the typed stored artifact's status is the current
decision. The outer envelope remains a compatibility projection updated by the
same writer. Do not create a third status copy. In this change, continue refusing
publication when those projections disagree, and make writer inconsistencies
visible in tests; do not guess approval from one of the two values.

### Alternatives considered

- **Only remove the strict comparison:** small, but a cancelled dataset still
  cannot enter the existing pending-review flow; insufficient.
- **Always extract again:** simple initially, but wastes work and risks duplicate
  output after interrupted saves; not recommended.
- **Remove most labels and migrate all artifacts now:** a broader schema and UI
  change affecting plans, analyses, uploads, exports, and historical checkpoints.
  Defer until there is evidence that the names themselves need replacement.
- **Recommended:** narrow storage comparisons, reuse compatible results, and use
  the existing extraction path with a fresh identity for incompatible results.

## 4. Exact target rules for A+B

Both reuse and fresh retry are required by this table. A recoverable response may
be appropriate for current source/storage failures, but it is not the substitute
for fresh extraction when only the old result is unusable.

| Previous result | Action in the current extraction request |
| --- | --- |
| No prior attempt or artifact | Create the initial dataset |
| Matching files and lineage, `pending_review` | Reuse the same ID; continue inspection/review |
| Matching files and lineage, `active` | Reuse the same ID; do not request pending review again |
| Matching files and lineage, `cancelled`, `rejected`, or `superseded` | Preserve it and create a new pending candidate |
| An unsupported extraction-result status, including dataset `draft` or `approved` | Treat the old result as unavailable for reuse and take the same bounded fresh-extraction path |
| Status history alone changed | Do not call this corruption; apply the current-status rule above |
| Prior registry missing | Recover a compatible unfinished/committed write from a verified journal as pending review; otherwise create a fresh candidate |
| Prior registry malformed or incompatible with the saved extraction | Do not reuse it; create a fresh candidate |
| Prior saved files or prior journal missing/corrupt/incompatible | Do not return the bad data; create a fresh candidate if the current approved extraction is valid |
| Existing unfinished attempt is compatible | Resume it using the existing journal |
| Current plan is not approved or current source/SQL is invalid | Follow existing plan/SQL handling; an old-result mismatch does not grant approval |
| Fresh extraction or save fails | Return its actual diagnostic; do not recursively create more candidates |

An explicit request to use an exact old dataset/version must not silently analyze
newly extracted rows under that old ID. Retrieval/analysis can report the missing
artifact reference as recoverable under existing stale-reference handling. this fix
preserve the separate file-load error policy in `resolve_dataset`; making those
load failures recoverable is deferred C. Successful fresh extraction returns its
new ID and requires the normal review for that new result.

A committed journal proves that files were saved, not that they were approved.
Hydration without a current registry decision must produce a pending result and
must not publish it or infer approval. Never use journal hydration to overwrite an
existing cancelled/rejected/superseded registry record. If an authoritative review
decision exists elsewhere, resolving that decision is a separate explicit step;
the journal alone cannot provide it.

## 5. Storage-content comparison

Create a small shared helper in `epi_agent/dataset_reuse.py`, named
`dataset_storage_content`, consumed by both the artifact store and extraction
replay. It takes a dataset record and returns a deep-copied comparison view.

Remove only current top-level `status` and nested
`provenance.status_transitions` from that view. Preserve all remaining fields,
including identity/version/kind, paths, row and column metadata, study/thread,
plan/SQL references, selected fields, SQL text, and other provenance.

Validate mapping shape before comparison. A malformed old record is unavailable
for reuse; it is not a reason for an uncaught TypeError/ValueError. Never mutate
the original snapshot or rewrite the original journal to match a review decision.

This deliberately starts with a narrow removal of mutable fields. Dropping all
provenance or comparing only status/ID would hide material mismatches.

Canonical on-disk metadata may still contain its original `pending_review` value.
That snapshot is a creation record. Validate it against its own saved manifest;
do not require it to become a second live copy of current review state.

## 6. Fresh-retry identity without another state machine

Retain the current deterministic base ID for existing extractions. Allocate fresh
attempts in that family as `<base-id>-retry-1`, `<base-id>-retry-2`, and so on.
The suffix is an operation identifier, not a review label.

Use existing registry records, persistence-attempt records, and the thread-scoped
`.persistence_attempts` journal directory to identify the latest family member.
Recognize only an exact base ID or the exact base plus a positive integer suffix.
Do not infer families from arbitrary prefix similarity or modification times.

Selection rules:

1. Determine the latest already-reserved family member from existing records and
   durable journals. An invalid journal still occupies its ID; never overwrite it.
2. Try to reconcile/reuse that member. Do not switch back to an older member merely
   because an old file was later repaired or an earlier review status changed.
3. If it is incompatible, reserve the next unused family member and execute once.
4. Write the existing begun-attempt journal before SQL execution. This provides
   the same identity after a process interruption or checkpoint rollback.
5. On resume, reconcile the newest reserved member. Do not generate a new ID just
   because its previous tool response was lost.

Preserve owner/thread path validation. Reuse the application's per-thread run
serialization; verify that this serialization covers reservation. If it does
not, reserve the journal exclusively and re-read a concurrent reservation before
execution. Do not assume the current overwrite-capable journal writer itself
provides an exclusive allocator.

At most one fresh extraction is started per tool invocation. Do not put the entire
extraction/commit path inside an unbounded retry loop. Filesystem failures during
family discovery/reservation are typed, recoverable persistence errors, not a
reason to retry in random directories.

## 7. Separate old-result reconciliation from current execution

Extract the old-attempt lookup/reconciliation portion of `_persist_extraction_result`
into a helper in `epi_agent/db_rag/tools.py`. It receives the selected dataset ID,
approved lineage, validated SQL, and authorized storage scope. It returns an
existing result, a compatible unfinished attempt, or a decision to start fresh.

Handle known old-result integrity/lifecycle failures only inside that boundary.
The fresh SQL execution, staging, promotion, and commit remain outside the fallback
catch. A successful current commit must not be duplicated because later response
formatting failed.

Check replacement controls before mutating their predecessor:

- A compatible unfinished replacement whose predecessor is still pending and
  whose feedback matches may continue under the existing replacement rules.
- A committed, reusable replacement does not need its predecessor to remain in
  the exact status it had at commit. Immutable saved lineage still matters.
- If a cancelled/superseded/missing predecessor makes an old replacement unusable,
  a fresh ordinary extraction may proceed from the still-approved current plan.
  It must not claim to supersede that predecessor, change its status, or reuse its
  review decision. Recompute ordinary extraction lineage with both predecessor
  fields absent/null, and clear replacement control together. Never retain
  predecessor-bearing lineage while dropping its required control record.
  Compute the ordinary base ID from that normalized request, then derive a stable
  recovery-family base from the ordinary base plus the failed replacement-family
  base. This avoids selecting an earlier ordinary dataset and makes repeated
  recovery of that replacement select the same new family. Apply section 6's
  reservation rules within this family. Keep the old candidate ID as diagnostic
  restart context, not as a predecessor requiring supersession.
- Missing/stale predecessor or feedback lookups must be handled explicitly;
  never allow their expected KeyError to become `INTERNAL_TOOL_ERROR`.

Lookup handling and ordinary fresh extraction belong to the same combined fix.
Test them separately during implementation and together at the tool boundary.

Do not change an artifact's study or silently reuse data across owners/threads.
Fallback uses the current validated request and its authorized source, never the
incompatible old record's source configuration.

## 8. Tool results and other artifact failures

`_dataset_result` must report the actual dataset status. An active reused dataset
should be returned with the references needed by existing output attachment and
completion routing. `_validate_and_extract` must preserve those result fields
when it adds the validated-SQL reference.

Update the DB-RAG instructions to explain that active reuse needs no new dataset
review, pending reuse still needs review, and a fresh result has a new identity.
These are existing workflow distinctions, not additional approval rules.
Prefer the existing prompt and accurate status in the existing tool-result format.
If instructions must change, limit the edit to these reuse/fresh-retry outcomes,
record the before/after prompt hash, and include it in evaluation comparison.
Do not adjust unrelated agent behavior or models as part of the fix.

Deferred C proposal: in `resolve_dataset`, make expected missing/unreadable old files a recoverable
tool outcome with the old ID and an actionable explanation. Do not automatically
replace the dataset inside an analysis operation: that would change its inputs
without updating provenance or review.

Do not broadly change `runtime.py` to swallow exceptions. Programming errors,
invalid current authorization, unavailable services, and cancelled active runs
must retain their appropriate handling. The goal is that normal artifact changes
never reach the unexpected-exception path.

## 9. Implementation tasks after approval

All steps below are pending. They are not authorization to start implementation.

### Task 1 — Isolate work and reproduce

- [ ] Confirm explicit approval to implement the combined fix. Record the frozen baseline and use
  the baseline/candidate protocol in section 13 before edits.
- [ ] Create branch `fix/dataset-reuse-recovery` in a separate worktree from the
  development checkout, using `.worktrees/dataset-reuse-recovery` if no preferred
  location is supplied. Check that location is ignored first.
- [ ] Copy the approved design/plan to that worktree. Keep changes off the user's
  current checkout and do not deploy to the Demo copy automatically.
- [ ] Use Python 3.12 and the existing dependency versions. Do not read or print
  credential values in diagnostics.
- [ ] Add `tests/test_dataset_reuse_recovery.py` with production-tool-boundary
  fixtures backed by a small real DuckDB source, real schema catalog, real artifact
  store, and temporary authorized thread storage.
- [ ] First reproduce cancelled replay as a failing test and record the exact
  failure before changing application code.

### Task 2 — Narrow comparison and compatible reuse

- [ ] Add `epi_agent/dataset_reuse.py` with the shared comparison view described
  in section 5 and focused tests for mutable versus immutable differences.
- [ ] Update both committed comparisons in `epi_agent/artifacts.py` and
  `epi_agent/db_rag/tools.py` to use it.
- [ ] Allow compatible active/pending reuse and repair result status/reference
  propagation. Verify that pending results are not published as active.
- [ ] Test actual status/result propagation against the existing prompt first;
  limit any required instruction edit to the combined behavior in section 8.
- [ ] Prove pending/active replay performs no second SQL execution.
- [ ] Convert ordinary extraction-replay status drift and expected stale
  predecessor lookups to recoverable outcomes without removing immutable checks.
- [ ] Verify the comparison step before adding fresh retry, then continue within
  the same approved scope. Do not release this partial step as the complete fix.

### Task 3 — One fresh retry when reuse fails

- [ ] Continue the combined implementation; no separate approval gate between A and B.
- [ ] Add family discovery/reservation helpers near the existing generated
  dataset path/journal helpers in `utils/dataset_artifacts.py`.
- [ ] Implement the bounded reconciliation boundary in `epi_agent/db_rag/tools.py`.
- [ ] Move begun journal persistence before execution for newly reserved attempts.
- [ ] Cover cancellation, rejection, supersession, mismatched records, missing
  files, malformed journals, lost responses, and checkpoint rollback.
- [ ] Preserve older artifacts byte-for-byte and preserve their recorded decisions.
- [ ] Verify serialization or exclusive reservation, including concurrent requests.
- [ ] Update `epi_agent/db_rag/prompt.py` only as required by section 8 and record its
  before/after hash as an evaluation-affecting input.

### Task 4 — Focused lifecycle regression coverage; defer broader cleanup

- [ ] Separate known replacement lifecycle drift from immutable checks and
  cover expected missing predecessor/feedback without terminal internal errors.
- [ ] Cover the ordinary fresh-extraction path when old replacement control is
  no longer applicable, including normalized lineage and stable new identity.
- [ ] Keep `epi_agent/datasets.py` load-error policy unchanged in this fix. Record its
  proposed change under C for later explicit review.
- [ ] Extend `tests/test_tool_failure_recovery.py` to exercise the actual new tool
  outcomes through runtime error handling. Retain fatal programming-error tests.
- [ ] Extend `tests/test_execution_artifact_lifecycle.py` with writer-consistency
  tests for native analysis envelopes, preserving existing publication protections.
- [ ] Change no analysis/publication status semantics in this fix. Newly discovered
  unrelated bugs are documented and proposed separately, not folded into the fix.

### Task 5 — Verification and review

- [ ] Run focused tests for the changes, then the existing scoped persistence,
  study-lineage, artifact lifecycle, root-state, and tool-failure regression suites.
- [ ] Add `scripts/smoke_dataset_reuse_recovery_real.py` for both reuse and fresh
  retry, as specified in section 11. Run once for at most five minutes, preserving
  evidence; focused tests cover individual comparison/recovery cases.
- [ ] Obtain an independent code review of the working-tree diff, with special
  attention to duplicate extraction, stale review controls, and cross-thread paths.
- [ ] Complete the baseline/candidate comparison and release gates in section 13.
  Review the complete A+B candidate; keep implementation and evaluation-adoption
  approvals distinct.
- [ ] Report changed files, test results, smoke outcome, and remaining limitations.
  Do not describe the separate Demo application as fixed until delivery and its
  verification have actually happened.

## 10. Target acceptance tests and scope

Run unchanged-path regression checks and all reuse/fresh-retry tests below for
the complete candidate. Analysis load-error recovery remains deferred and must
not be used as an acceptance criterion requiring broader changes.

| Test | Required observable result |
| --- | --- |
| Pending replay | Same dataset ID and contents; no second SQL execution |
| Dataset already reviewed and `active` | Same dataset ID; actual active status; no pending-review interrupt |
| Cancelled/rejected/superseded replay | Fresh pending dataset; original preserved; no false terminal collision |
| Status-history-only difference | Storage comparison matches; lifecycle rule selects the next action |
| Changed study, SQL, selected columns, or paths in old metadata | Old data refused for reuse; fresh result only from the current authorized plan |
| Missing/corrupt old parquet/schema/metadata/journal | Preserve integrity checks; one fresh extraction; old evidence preserved |
| Missing old registry entry | Verified journal recovery or fresh extraction; no uncaught lookup failure |
| Committed journal with no current registry approval | Recovered result remains pending; never infer active/approved from committed storage |
| Restart response lost | same reserved retry ID is returned on replay |
| Checkpoint predates restart reservation | durable journal selects the latest retry, not the original failed ID |
| Older artifact repaired after restart | latest reserved attempt remains selected |
| SQL/save failure in fresh attempt | correct diagnostic; no recursive fresh-attempt loop |
| Replacement predecessor approved/cancelled/removed | Compatible reuse or ordinary fresh extraction when needed; no false supersession/internal error |
| Ordinary restart from a replacement | ordinary lineage and controls agree; stable recovery family; repeated calls find the same new attempt |
| Two requests for the same thread/extraction | Serialized or exclusive allocation; no duplicate write under one ID |
| Same request in another owner/thread | Independent storage and attempt family |
| Active artifact export/attachment | Correct dataset reference delivered; pending data stays unpublished |
| Native analysis status transition | Outer envelope and typed status update together |
| Stale analysis output/reference | Recoverable error; review/publishing approval is not inferred |
| Missing requested analysis dataset file | this fix: preserve existing policy and do not silently substitute; C proposal: recoverable result identifying the missing input |
| Unexpected programming exception | Existing runtime terminal behavior remains tested |

Do not make network calls to LLM providers in unit tests. Stubs in focused unit
tests may observe call counts or inject failures, but are not substitutes for the
dedicated real smoke below.

## 11. Dedicated real feature smoke

Plan the combined `scripts/smoke_dataset_reuse_recovery_real.py` now; create/run
it only after implementation approval. It covers the complete user-visible reuse
and fresh-retry behavior in one real smoke. Focused production-boundary tests
separately cover pending reuse, immutable checks, and interruption cases. Inspect
raw calls/state so an answer from model memory cannot count as proof of reuse.

Use a temporary runtime and an installed study package. Launch the real FastAPI
backend serving the compiled TypeScript frontend, using the repository's existing
browser-runner helpers. Use configured real providers; do not stub the LLM,
embeddings, DB-RAG, LangGraph, backend, or browser interactions.

1. Submit a small dataset request through the browser and complete the normal
   plan review.
2. At dataset review, capture its ID and saved state, then cancel using the UI.
3. Submit the same extraction request in the same conversation.
4. Assert a fresh dataset reaches review, its ID differs, and the old dataset
   remains cancelled. Confirm no terminal identity-collision/internal error in
   raw state and no error presentation in the page.
5. Approve the new dataset, request reuse, and confirm an active saved result is
   returned without another extraction or pending-review interruption.

Retain backend logs, sanitized raw state, browser text, screenshot, and exception
traceback on failure/timeout. Assert actual extraction tool calls and persisted
attempt IDs: a model that merely answers from memory does not prove reuse.

The smoke runs once with a five-minute deadline. Failure or timeout is reported,
not retried automatically. Backend-only corruption/rollback cases are verified
by the focused production-boundary tests. No UI code change is currently planned;
if one becomes necessary, rebuild the production bundle and verify its manifest
as AGENTS.md requires.

## 12. Compatibility, delivery, and approval boundary

No destructive migration, existing-record relabeling, or conversation deletion is
needed. Old deterministic IDs and journals remain readable. The fresh-retry
suffix is new, so verify all ID/path/export consumers before the fix is considered
complete. Use existing record/journal schemas; no bulk migration is planned.

The development repository ignores `/docs/`, `/tests/`, and `/scripts/`. Keep
these approval documents available locally. At implementation time, preserve the
approved plan, tests, and smoke alongside the worktree delivery explicitly; do
not silently change the repository-wide tracking policy or assume ignored files
will travel in a commit.

The source checkout is `/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent`.
The original failing thread is in `/Users/xutaowang/Desktop/Demo/Epi-AI-Agent`.
Do not edit that thread's database or ship code to the Demo checkout as part of
documentation approval. After implementation, report source verification and
Demo delivery separately.

Implementation approval covers Tasks 1–5 as one combined A+B fix. A global
artifact-status migration, human-checkpoint changes, and a global exception-
swallowing policy remain outside the scope. Broader cleanup is deferred and is
not assumed to be a required next project.

## 13. Evaluation protection and evidence required for release

### Baseline capture after implementation approval, before edits

Record the source commit/branch and dirty-file inventory. Git alone is insufficient:
the repository ignores tests, scripts, docs, and runtime inputs. Record hashes of
the test/smoke assets actually used, prompts/tool schemas, compiled frontend,
dependency lockfiles or installed versions, study package/source snapshot versions,
and non-secret model/provider settings. Record relevant evaluation task definitions,
random seeds where supported, and cache configuration. Never export API keys.

The known Demo checkout is not automatically the evaluator. Identify and record
the actual evaluation checkout and runtime before testing or delivery. Keep the
Demo and live evaluation state read-only during all candidate verification.

Use a separate candidate worktree and runtime. If reproducing from a historical
SQLite state, use a consistent read-only SQLite backup/export into isolated
storage rather than copying only the database file while WAL writes are active.
Resolve copied artifact paths into isolated test storage; reject any fixture that
would write back to the original runtime. Do not load environment defaults that
redirect tests into live dataset/output directories.

### Controlled regression comparison

| Area | Comparison / pass condition |
| --- | --- |
| Ordinary extraction | Same approved source/SQL/selected columns, row count, schema, and values; no extra SQL calls or reviews |
| Valid pending replay | Same ID, no SQL re-execution, same review eligibility and publication visibility |
| Valid active replay | Same ID/data, accurate active status, no accidental pending-review transition |
| Cancelled replay | One fresh pending result; original bytes/decision preserved |
| Review/version boundary | Approval remains scoped to the exact result; revised data/figures do not inherit prior approval |
| Analysis/figures/tables | Existing run/output linkage, atomic decisions, publication controls, and exports preserved |
| CSV export | Same content/schema for unchanged inputs; export creates no extra review requirement |
| Integrity and ownership | Tampered lineage/paths/versions/owners are never accepted for reuse |
| Interruptions and cancellation | Existing active cancellation still stops execution; resume reserved attempts without duplicate results |
| Compatibility | Old checkpoints/journals/export routes remain readable; candidate writes only isolated storage |
| Cost and performance | Matching replay adds no model/SQL calls; fresh retry executes once only when justified; record latency/calls |

Use the same frozen fixtures and non-secret configuration for baseline/candidate
runs. For deterministic tool-boundary tests, assert semantic data equality and
operation counts. Compare file bytes where they are supposed to be unchanged,
especially preserved original artifacts. Do not demand byte-identical newly
generated timestamps/IDs, or identical live LLM wording.

Live-provider tests provide integration evidence, not deterministic proof of
unchanged evaluation scores. Record model-reported versions, provider settings,
timestamps, and variability. A silent provider update limits comparability and
must be reported. Do not rerun or amend the official frozen evaluation simply to
make a candidate appear comparable.

### Release gate

All of the following are required before proposing adoption:

- The new regression test failed for the intended reason before implementation
  and passes on the candidate. For already-working controls, both baseline and
  candidate pass.
- Relevant unit/production-boundary suites pass; no unrelated failures are
  concealed by changing assertions or silently changing baseline configuration.
- Negative integrity, exact-version review, publication, cancellation, and
  owner/thread isolation tests pass.
- The dedicated real smoke succeeds once within its five-minute limit. On
  failure/timeout/missing credentials, preserve evidence and mark release blocked;
  do not rerun automatically or substitute a mocked run.
- Independent review reports no unresolved critical/important findings, and the
  diff contains only the approved combined A+B scope.
- Baseline/candidate evidence records expected behavior changes, unexpected
  differences, calls/latency, and verification limitations. Unexplained regressions
  block adoption. A live smoke alone is not sufficient release evidence.
- The user explicitly approves adoption, or evaluation finishes and the user
  approves the normal post-evaluation release. No automatic deployment follows
  design approval, implementation approval, or passing tests.

### Rollback and evaluation reporting

Keep the baseline checkout/runtime available and immutable. Candidate evaluation
uses its own state directory and outputs. Rollback selects the baseline code and
baseline runtime together; never point older code at candidate-only journals and
assume reverse compatibility. Preserve candidate evidence separately.

If an exception is approved during the freeze, record the change boundary and
configuration/commit for each evaluation result. Do not pool pre-change and
post-change scores as one unchanged system. Record any comparison as a separately
identified candidate evaluation under the agreed protocol.

No design or test suite can guarantee the absence of new bugs. These gates reduce
and expose risk. Until they pass, describe the candidate as unverified or blocked
for release, and keep the evaluation baseline unchanged.
