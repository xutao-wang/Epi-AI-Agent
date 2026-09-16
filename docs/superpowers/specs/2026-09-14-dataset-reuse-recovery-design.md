# Dataset reuse and lifecycle separation during the evaluation freeze

Status: combined A+B reuse/fresh-retry design direction agreed. Implementation
remains pending explicit approval of the updated detailed proposal; broader
artifact cleanup is deferred. No application
or test code may be changed before that approval. Implementation must use a
separate worktree.

Detailed proposal: [implementation plan and artifact-label audit](../plans/2026-09-14-dataset-reuse-and-artifact-lifecycle.md).

## Evaluation freeze and scope

This revision incorporates the user's requirement to protect the evaluation
baseline. It authorizes documentation only. No implementation, worktree creation,
runtime migration, deployment, or evaluation rerun is authorized by this revision.
Implementation requires explicit approval and a separate worktree; adoption into
the evaluation system requires a separate explicit decision after verification.

Keep the evaluation checkout, prompts, models, installed study versions, runtime
data, and current results unchanged. Do not mix candidate results into the frozen
baseline. No testing against the live evaluation thread or writes to its database.

The intended principle is:

**Tools create versioned artifacts. Review records decisions about exact artifacts
or actions. The runtime tracks execution and saving progress.**

This separates responsibilities, not necessarily services or database tables.
Reuse the current implementation boundaries; do not add three new subsystems.

## Shared vocabulary and artifact formats

Artifact kind (dataset, figure, table, analysis run, plan), file format (CSV,
Parquet, PNG, JSON), review decision, and operation progress are separate facts.
The shared status vocabulary is not a mandatory sequence for every artifact.

Preserve existing serialized labels during the freeze. `active` is not universal
proof of human approval: some supporting artifacts receive it when created.
Approval must refer to an exact artifact/action and version. An analysis review
can govern its linked figures and tables as one result. Exporting a dataset to CSV
does not require a second independent dataset or review lifecycle.

Cancellation stops an operation; it does not establish that saved bytes are
corrupt. For compatibility, existing cancelled artifact records remain cancelled;
do not bulk relabel them. Storage snapshots may retain their original creation
status without becoming authoritative current review decisions.

## One focused fix using existing records

A+B is one complete regression fix: an old dataset must not block a valid new
extraction request. A decides whether saved data can be reused; B creates a fresh
retry when it cannot. These are implementation steps within one scope, not
separate features or approval gates. Completing A alone does not complete the fix.

A fresh retry has a new dataset identity and its own review outcome, preserves the
old result, and may use the still-valid approved plan. It does not inherit the old
dataset's approval. If interrupted, it resumes the same reserved attempt rather
than creating another retry.

Use the current artifact records and persistence journals. Introduce no new
artifact framework, stored status labels, or review subsystem. Broader cleanup
remains a documented possibility, not an assumed mandatory follow-up project.

This fix helps later simplification by separating storage comparison from review
state, placing reuse/restart selection at one boundary, and adding regression
tests that protect those responsibilities. It does not remove all duplicated
statuses or standardize every artifact type now.

## Problem and requested behavior

Thread `a9a10994715544d4a624c5f5d18a2ccf` saved an extraction successfully,
then cancelled its dataset review. Replaying extraction compared the mutable
dataset registry with the original persistence snapshot and raised a fatal
`DATASET_IDENTITY_COLLISION`. Only status and status-transition history differed.

The requested behavior is to reuse compatible saved data and automatically start
a fresh extraction attempt when a previous result cannot be reused. A mismatch
in the previous result must not, by itself, terminate the current request.

## Approaches

1. Recommended: narrow reuse validation and add an automatic fresh-attempt
   fallback. This preserves useful saved work and addresses incompatible results.
2. Always extract again. This avoids reuse decisions but repeats work and loses
   retry deduplication unless separately handled.
3. Only relax the comparison. This fixes false corruption reports but leaves
   cancelled datasets rejected by the existing pending-review requirement.

## Target behavior of the combined A+B fix

- Check stable dataset identity, source/plan/SQL lineage, and saved-file integrity.
  Exclude current review status and status-transition history from the original
  persistence-snapshot comparison. Do not ignore arbitrary provenance changes.
- Reuse a matching pending dataset and continue its normal inspection/review path.
  Reuse a matching active dataset without trying to review it as pending again.
- For a cancelled or superseded result, or an incompatible/missing previous
  artifact, start a new extraction attempt automatically. Preserve the previous
  dataset, files, and decisions. The new candidate gets its own ID and proceeds
  through the existing inspection and review path.
- A fresh attempt means a new dataset operation within the same conversation.
  Retain the current request and a still-valid approved plan; do not clear the
  conversation or fabricate a new approval. Changed extraction requirements must
  still follow existing plan validation and review requirements.
- Persist the fresh attempt identity before extraction so a retry resumes that
  attempt rather than repeatedly creating new datasets. A new attempt must not
  resolve back to the incompatible deterministic ID.
- Choose the fallback before committing or returning a dataset. Do not implement
  a global exception handler that restarts arbitrary tools or duplicates a
  successful current operation.
- Allow one automatic fresh start for a failed reuse decision in an invocation.
  Failures in the fresh execution use the existing execution error handling;
  missing source data or a failed write cannot be made successful by relabeling it.
  “Any mismatch means restart” is not the rule: distinguish an unusable prior
  result from a failure of the current source, validation, or storage service.
- Return successful reuse or successful fresh extraction as normal tool results.
  Retain the reason for abandoning reuse in internal diagnostic information.

## Implementation scope after explicit approval

Update reuse validation and attempt selection in `epi_agent/db_rag/tools.py`,
the persistence/artifact-store helpers needed to retain a fresh attempt identity,
and downstream routing so active results are not sent for pending review.
Keep review requirements unchanged. Do not modify the stored demonstration thread.
Do not rename statuses, migrate persistence schemas, change models, or rewrite
publication rules. Limit any tool-instruction adjustment to explaining reuse and
fresh retry, and record it as an evaluation-affecting change. Verify comparison
and retry steps incrementally, then review and verify the complete fix together.

Development source is this Epi-AI-Agent checkout. The original failing thread was
in the separate `/Users/xutaowang/Desktop/Demo/Epi-AI-Agent` checkout; source
implementation and delivery to that running copy must be reported separately.

## Verification

Before implementation, record the approved baseline commit, dirty-file inventory,
ignored runtime/configuration inputs, and dependency/model/study versions without
copying credentials into reports. Use isolated test storage and the same frozen
inputs for baseline/candidate comparisons. Existing pending replay, extraction,
review, cancellation, analysis, figure delivery, and CSV export must retain their
expected behavior outside the named regression cases.

- Regression: replay after review cancellation automatically returns a fresh
  pending candidate, preserves the cancelled original, and avoids a false terminal
  collision. A recoverable response alone does not satisfy this regression.
- Valid pending replay reuses the same dataset without re-executing SQL.
- Valid active replay remains active and does not request pending review.
- Status-history changes alone are not classified as data corruption.
- incompatible old lineage or missing/damaged saved files trigger a separate fresh
  extraction and never return the incompatible data as valid.
- a retry after interruption of the fresh attempt resumes its persisted identity.
- fresh-execution failure does not enter an unbounded restart loop.
- Run focused production-boundary tests and the dedicated real feature smoke
  required by AGENTS.md, including rendered review/continuation behavior where
  the feature affects the browser. Preserve failure evidence and run the smoke
  once with its five-minute limit.
- Test negative cases proving source/SQL/path/owner/version mismatches remain
  rejected for reuse, pending results remain unpublished, and cancellation still
  stops active execution.
- Review incremental changes and verify the complete A+B fix. A failed/missing required check
  prevents release; do not rerun a failed real smoke automatically.
- Keep baseline and candidate runtime data separate so rollback switches back to
  the untouched baseline without converting candidate journals or artifacts.

No design review or test suite can guarantee zero new bugs. Evidence required for
release is specified in the detailed proposal; until it exists, the status is
unimplemented/unverified, not safe for evaluation deployment.
