# Evidence-First Clarification Design

**Date:** 2026-08-20

## Goal

Replace lexical blocking of technical-looking clarification text with an
evidence-first agent policy. The agent should use the designated evidence
sources before asking the user, while retaining the ability to ask whenever
human intent or knowledge is genuinely necessary.

## Problem

The shared `general-request_clarification` tool currently concatenates its
question, reason, and option labels and searches that prose for fixed technical
terms such as `schema`, `table`, and `identifier`. Any match produces
`TECHNICAL_CLARIFICATION_DEFERRED` instead of displaying the clarification.

This protects users from being asked to resolve internal database mechanics,
but it classifies questions from individual words rather than their meaning.
A valid scientific or study-routing clarification can therefore be blocked
merely because its natural wording contains a technical-looking term.

## Behavioral Policy

Clarification remains one general, open-ended capability. It will not acquire a
fixed clarification-kind enumeration or a replacement keyword classifier.

Before asking a question, the agent must decide semantically whether an
available designated source can resolve the uncertainty. For participant
database questions, this means searching the runtime catalog, inspecting
plausible tables, and checking relationship paths as applicable. Other
workflows must likewise use their relevant evidence tools before transferring
an answerable investigation to the user.

After the applicable investigation:

- If the evidence resolves the uncertainty, the agent continues without asking.
- If the uncertainty requires human intent or knowledge, the agent requests one
  concise clarification with concrete options.
- If the evidence demonstrates a technical limitation and user input cannot
  resolve it, the agent reports the limitation without guessing.
- If the user could reasonably supply missing information, such as the meaning
  of a user-provided column, the agent may ask after inspecting the available
  evidence.
- The agent must not repeat a clarification that the user has already answered
  or that subsequent evidence has resolved.

The policy intentionally distinguishes internal mechanical lookup from
user-owned meaning. An installed study's join key is an internal lookup; the
intended interpretation of an unfamiliar user-supplied field can be a valid
human clarification.

## Backend Design

In `epi_agent/tool_packs/general/clarification.py`:

- Remove `_TECHNICAL_CLARIFICATION_PATTERN` and all of its terms.
- Remove `_is_technical_clarification`.
- Remove the `TECHNICAL_CLARIFICATION_DEFERRED` branch from
  `RequestClarificationTool.invoke`.
- Preserve validation of the question, reason, fixed options, unique IDs, and
  unique labels.
- Preserve interrupt, cancellation, answer, and clarification-exchange behavior.

The existing agent-delegation option validation remains. The model must provide
only concrete options; the user interface supplies exactly one standard
`Let the agent decide` option. This validation is unrelated to technical-text
classification and prevents duplicate delegation choices.

No API or frontend payload changes are required. The interrupt continues to
contain `type`, `question`, `reason`, and `options`.

## Agent Instructions

Update the core agent and DB-RAG instructions so that behavior is governed by
the evidence-first order rather than by the clarification tool inspecting
words.

The shared instructions must state that the agent:

1. uses applicable designated evidence tools before asking about facts those
   tools can establish;
2. asks when human intent or knowledge is genuinely required;
3. never guesses merely to avoid clarification;
4. reports a demonstrated technical limitation when user input would not make
   progress; and
5. does not repeat an answered or resolved clarification.

DB-RAG-specific instructions continue to name the catalog, table inspection,
and relationship tools as the required sources for schema resolution. The
current absolute instruction to end every exhausted schema search with a
technical failure should be relaxed only enough to permit a question when the
user could reasonably provide the missing information. It must not permit the
agent to ask users for internal table names, column identifiers, or join keys
that the installed package is responsible for defining.

## Data Flow and Error Handling

The normal flow remains:

1. The core model receives the user's request and available study evidence.
2. It invokes the applicable evidence tools when the uncertainty is
   tool-resolvable.
3. It either continues with the evidence, reports a proven limitation, or calls
   `general-request_clarification` when human input is useful.
4. The clarification tool validates the structural option contract and pauses
   the same reasoning loop.
5. The user's answer resumes that loop and is recorded in clarification history.

The clarification tool no longer emits `TECHNICAL_CLARIFICATION_DEFERRED` based
on prose. Ordinary argument-validation errors and the existing cancel and
invalid-resume behavior remain unchanged.

## Testing

Focused unit tests will:

- remove the expectation that technical keywords produce
  `TECHNICAL_CLARIFICATION_DEFERRED`;
- prove that otherwise valid questions containing `table`, `schema`,
  `identifier`, and related terms reach the clarification interrupt unchanged;
- retain coverage for invalid option counts, duplicates, blank text,
  agent-delegation duplicates, cancellation, normal answers, and delegated
  answers; and
- verify that the public clarification payload contract remains unchanged.

Prompt-contract and agent-level tests will verify that:

- an obvious installed-study schema uncertainty causes the relevant catalog or
  relationship investigation before any clarification;
- a genuine scientific ambiguity can request clarification;
- ambiguous semantic study routing can request clarification even when its
  natural wording contains a former blocked term;
- an exhausted internal schema investigation produces an honest technical
  limitation instead of an invented mapping; and
- missing user-owned semantics may be clarified after the available evidence
  has been inspected.

Existing API and frontend clarification tests should continue to pass without
contract changes.

## Scope

This change does not redesign the clarification interface, introduce fixed
clarification categories, alter study selection, or change catalog and
relationship algorithms. It changes only the technical-question guardrail from
lexical blocking to evidence-first semantic agent behavior.
