# Full-Overview Study Routing Design

**Status:** Approved design, pending implementation plan

**Date:** 2026-08-19

## Relationship to the Existing Multi-Study Design

This specification supersedes the study-discovery and study-selection portions of `2026-08-18-multi-study-agent-selection-design.md`. It preserves that design's downstream requirements: every study-dependent call carries an exact `study_id`, study-specific resources remain isolated, and generated artifacts retain study provenance.

The previous unranked, paginated `search_studies` flow is removed. Study routing becomes a direct responsibility of the core agent, using the user's request and the complete authoritative overview of every currently installed study.

## Problem

The current `search_studies` tool accepts only `offset` and `limit`. It returns studies in a stable but non-semantic order and truncates each overview. Consequently, it cannot assess scientific applicability. In the observed failure, the agent received the first page, selected the first study, and began DB-RAG retrieval even though the user's request was ambiguous and the omitted portion of the overview contained relevant routing guidance.

This failure is architectural rather than a missing keyword rule. Adding string matches for common variables or diseases would be brittle, would not generalize to future study packages, and would mix scientific scope with physical schema discovery.

## Goals

- Let the core agent determine whether a database request is scientifically supported by an installed study before invoking DB-RAG or study-design retrieval.
- Base that decision on the current user request and complete, authoritative study overviews.
- Clarify when multiple studies are plausible and decline when none is applicable.
- Work for arbitrary future studies without hard-coded study names, counts, diseases, populations, or variable names.
- Keep study routing distinct from deeper study-design reasoning after a study has been selected.
- Preserve exact study scoping, resource isolation, and provenance in all downstream calls.

## Non-Goals

- Building a second semantic-search or ranking service for study selection.
- Assigning numerical relevance scores or a fixed dominance threshold.
- Inferring applicability from catalog column names.
- Automatically selecting the only installed study solely because it is the only one.
- Changing the study package manifest format or adding a separate routing document.
- Making study selection sticky across unrelated requests.
- Redesigning the existing clarification user interface.

## Chosen Architecture

For every core-agent invocation, runtime builds a dynamic installed-study context from the live `StudyRegistry`. For each registered study, it includes:

- the exact study ID used by downstream tools;
- the current package label;
- the complete contents of the package's authoritative `overview.md`;
- an explicit availability status if the overview cannot be loaded.

The core agent evaluates the current user request against this context before making any study-dependent tool call. There is no preliminary `search_studies` call, pagination, ranking, or arbitrary candidate cutoff.

The high-level flow is:

1. Runtime reads the live registry and constructs the full installed-study context.
2. The core agent classifies the request as non-study-dependent or study-dependent.
3. For a study-dependent request, it determines whether zero, one, or multiple installed studies are scientifically applicable based on their overviews.
4. It either declines, requests clarification, or invokes the next capability with one exact `study_id`.
5. Only after routing may DB-RAG verify field availability or study-design search retrieve detailed evidence for that selected study.

## Prompt Responsibilities

### Core instructions

`GENERAL_CORE_INSTRUCTIONS` continues to define general behavior, attachment handling, analysis safety, and tool-use rules. It should not contain package-specific study facts.

### Study routing prompt

A new `STUDY_ROUTING_SYSTEM_PROMPT` belongs to the studies tool pack and is always included in the assembled core system prompt. It defines the routing decision contract and teaches the model how to use the installed-study context.

It must state that:

- applicability is a semantic judgment based on the complete overview, not a string match against a fixed vocabulary;
- the number or order of installed studies is not relevance evidence;
- phrases such as “my database,” a previously selected study, a default, or registration order are not sufficient routing evidence;
- an explicit study name does not override a clear scientific incompatibility described by its overview;
- catalog field availability must not be used as a substitute for scientific applicability;
- each new request is routed independently unless the current conversational request itself clearly refers to an immediately preceding clarification;
- downstream study-dependent tools may be called only after one exact applicable study has been resolved.

### Study-design prompt

`STUDY_DESIGN_SYSTEM_PROMPT` remains separate and retains its current role: reasoning about design evidence within an already selected study. It is included only when study-design search is available.

It must not select among installed studies. Its retrieval tools continue to require the exact selected `study_id`, and its evidence rules remain unchanged: study-design evidence describes meaning and methodology but does not prove that a physical field exists.

### Other capability prompts

Publication, DB-RAG, and other capability prompts retain their current responsibilities. The assembled prompt order is:

1. `GENERAL_CORE_INSTRUCTIONS`
2. `STUDY_ROUTING_SYSTEM_PROMPT`
3. publication instructions
4. `DB_RAG_SYSTEM_PROMPT` when DB-RAG is available
5. `STUDY_DESIGN_SYSTEM_PROMPT` when study-design search is available

These are sections of the one core agent's system instruction, not separate agents.

## Dynamic Installed-Study Context

The existing ID-and-label directory is replaced by a complete overview context. Runtime must derive it fresh from the live registry so package installation, removal, relabeling, or overview changes are reflected without editing prompt source code.

The representation must have unambiguous boundaries and machine-visible metadata, for example one structured block per study containing the exact ID, label, overview status, and overview text. Stable ordering may be used for reproducibility, but the routing prompt must explicitly say that order has no relevance meaning.

No overview may be truncated, summarized, paginated, or omitted because of its position. The package installer already bounds an individual overview at 32 KiB. If the combined installed-study context exceeds the supported model input budget, runtime must fail visibly with a configuration error. It must not silently keep an arbitrary subset, because doing so recreates the original routing bug.

## Routing Decision Contract

### Exactly one applicable study

The core agent may proceed to the relevant study-dependent capability using that exact `study_id`. Scientific applicability comes from the overview; physical field existence is checked afterward through that study's DB-RAG catalog.

Being the sole installed study is not sufficient. A single installed tuberculosis cohort, for example, must not be selected for an unrelated cancer extraction request unless its authoritative overview actually supports that scope.

### Multiple plausible studies

The core agent calls `general-request_clarification` and does not call DB-RAG, study-design search, extraction, SQL, or custom analysis in the same decision step. The clarification should present the live candidate labels and the concise scope distinctions derived from their current overviews.

Generic analysis concepts may occur across many studies. The implementation must not encode a list of such concepts; ambiguity is judged from each package's scientific description.

### No applicable study

The core agent does not call a study-dependent tool. It gives a scope-incompatibility response that:

- states that no installed study supports the requested database analysis;
- dynamically lists the currently installed studies using their live labels and concise scope descriptions derived from their complete overviews;
- invites the user to refine the question to a supported installed scope, install an appropriate study package, upload a relevant dataset, or ask a non-database question.

The study names, number of studies, and scope text in this response must never be fixed in prompt text or application code. They are generated from the live registry context, so adding or removing studies automatically changes the choices shown to the user.

These alternatives are informational; they do not authorize the agent to force an incompatible request onto one of the listed studies.

### No studies installed

The core agent explicitly states that participant-database search and extraction are unavailable because no study database is installed. It may suggest installing a study package, uploading a relevant dataset, or asking a general or literature-based question. It must not fabricate installed choices.

The empty state is also derived from the live registry rather than a hard-coded study list.

### Missing or unreadable overview

A study whose overview is missing or unreadable cannot be established as scientifically applicable and must fail closed. The dynamic context identifies the affected live study and says its routing evidence is unavailable. The agent must not prefer another study merely because that other overview loaded successfully.

If the unavailable overview prevents a sound zero/one/many decision, the agent explains the configuration problem and does not invoke a study-dependent tool.

### Non-study-dependent requests

General explanations, literature questions, and other requests that do not require an installed participant database may use the appropriate non-study capability without forcing study selection.

## Scope and Field Availability Are Separate Gates

Routing answers: “Which installed study, if any, scientifically supports this request?” This is decided from `overview.md`.

DB-RAG answers: “Does the selected study physically expose the fields and relationships required to perform the analysis?” This is decided from the selected study's catalog and relationship metadata.

Passing the routing gate does not guarantee field availability. Failing the routing gate forbids catalog exploration of unrelated studies merely to look for similarly named fields.

## Tool and Package Changes

- Remove `search_studies` from the studies tool pack and from the tool schemas bound to the core agent.
- Remove its `offset`/`limit` argument model, pagination behavior, overview truncation, and tests that codify those behaviors.
- Add the routing prompt module to the studies tool pack and include it in core prompt assembly.
- Replace the minimal installed-study directory renderer with the full-overview renderer.
- Keep existing exact-`study_id` arguments on every downstream study-dependent tool.
- Keep package manifest formats unchanged and reuse each package's complete existing `overview.md`.
- Keep the existing selected-study `STUDY_DESIGN_SYSTEM_PROMPT` and study-design retrieval implementation.

If the studies tool pack has no callable discovery tools after this change, it may remain a prompt/context capability rather than an empty tool provider.

## Security and Provenance

Overview text is package-supplied evidence, not an instruction channel. It must be delimited as data, and the system routing prompt has authority over any instruction-like text inside an overview.

All downstream operations retain the resolved exact `study_id`. SQL, extracted datasets, Python analyses, figures, and exported artifacts continue to report their study provenance. No cross-study resource fallback is introduced.

## Verification Strategy

### Unit and integration tests

Tests use fictional study packages and invented concepts so they prove semantic routing behavior without depending on hard-coded epidemiology terms. Required cases include:

- an ambiguous generic database request across two plausible fictional studies requests clarification and makes no study-dependent call;
- a scoped request supported by exactly one overview selects its exact study ID;
- an unsupported request with one installed study makes no DB-RAG or study-design call;
- an unsupported request with multiple installed studies makes no study-dependent call;
- an explicitly named but scientifically incompatible study is not used;
- a general non-database question does not trigger study routing tools;
- reversing study registration order produces the same routing result;
- with more than five installed studies, an applicable later study remains visible and selectable;
- relevant guidance near the end of a long overview is present in model context;
- no overview content is silently truncated;
- a missing or unreadable overview fails closed;
- an empty registry produces the explicit no-studies response;
- negative and clarification responses update when the live registry's studies and labels change;
- the bound core tool schemas no longer expose `search_studies`, `offset`, or `limit`.

Prompt-level agent tests should inspect tool calls as well as final prose. The critical safety assertion is the absence of any DB-RAG, study-design, extraction, SQL, or custom-analysis call before routing resolves one applicable study.

### Dedicated smoke test

Add an executable smoke under `scripts/` following repository guidance. It must exercise real production dependencies and the real FastAPI/core-agent path, including the existing TypeScript clarification UI where user-visible behavior is involved. It must not replace production components with local stubs.

At minimum, the smoke verifies an unsupported database request against installed fictional studies and asserts from raw activity/state that no study-dependent retrieval occurred. It should also verify that the response or clarification choices reflect the live installed registry. Run it once with a maximum duration of five minutes.

## Acceptance Criteria

The design is implemented when:

- the core model sees every installed study's complete authoritative overview on every core-agent invocation;
- `search_studies`, its pagination interface, and overview truncation are gone;
- routing uses no hard-coded study, disease, population, or variable vocabulary;
- zero, one, and multiple-applicability outcomes follow the contract above;
- negative and clarification messages always reflect the live installed registry rather than fixed examples;
- a sole but incompatible study is never automatically selected;
- DB-RAG and study-design retrieval begin only after one applicable study is resolved;
- `STUDY_DESIGN_SYSTEM_PROMPT` remains focused on the selected study and is not merged with routing;
- exact study scoping and provenance remain intact; and
- automated tests and the dedicated real smoke pass.
