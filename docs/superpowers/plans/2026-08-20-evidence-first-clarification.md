# Evidence-First Clarification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace keyword-based technical clarification blocking with an open-ended clarification tool governed by evidence-first semantic agent instructions.

**Architecture:** Keep the existing shared clarification interrupt and UI contract unchanged. Remove only the technical prose classifier from the tool, then make the core and DB-RAG prompts require designated evidence lookup before asking while allowing questions when human intent or knowledge is genuinely needed.

**Tech Stack:** Python 3, Pydantic, LangGraph interrupts, pytest, React/Vitest regression checks, existing real FastAPI/Playwright smoke runners

## Global Constraints

- Do not introduce a fixed `clarification_kind` enumeration or a replacement keyword classifier.
- Remove every term in `_TECHNICAL_CLARIFICATION_PATTERN`, the `_is_technical_clarification` helper, and the `TECHNICAL_CLARIFICATION_DEFERRED` branch.
- Keep the shared clarification question, reason, fixed-option, interrupt, cancellation, answer, and clarification-exchange contracts unchanged.
- Keep agent-delegation option validation; the model supplies concrete options and the UI supplies exactly one `Let the agent decide` choice.
- Require applicable designated evidence lookup before asking about facts those tools can establish.
- Permit clarification when human intent or knowledge is genuinely required; never guess merely to avoid asking.
- Report a demonstrated technical limitation when user input cannot resolve it.
- Do not redesign study selection, catalog search, relationship algorithms, the API schema, or the frontend.

---

## File Structure

- `epi_agent/tool_packs/general/clarification.py` — retains only structural clarification validation and interrupt/resume behavior; no longer classifies prose as technical.
- `tests/test_epi_agent_clarification.py` — proves former technical keywords have no classification effect and preserves the existing structural contract coverage.
- `epi_agent/agent.py` — owns the workflow-neutral evidence-first clarification policy.
- `epi_agent/db_rag/prompt.py` — specializes the policy for catalog, table, field, and relationship investigation.
- `tests/test_epi_agent_registry.py` — locks the workflow-neutral prompt contract.
- `tests/test_multi_study_db_rag_tools.py` — locks the DB-RAG investigation order and user-helpfulness boundary.

### Task 1: Remove lexical classification from the shared clarification tool

**Files:**
- Modify: `tests/test_epi_agent_clarification.py`
- Modify: `epi_agent/tool_packs/general/clarification.py`

**Interfaces:**
- Consumes: `RequestClarificationArguments(question: str, reason: str, options: list[ClarificationOptionArguments])`
- Produces: unchanged `RequestClarificationTool.invoke(arguments: dict[str, Any], context: ToolContext) -> ToolResult`
- Preserves: `AGENT_DECIDE_ANSWER`, `agent_clarification` interrupt payload, and all option validation

- [ ] **Step 1: Replace the technical-deferral test with a failing no-classification regression test**

In `tests/test_epi_agent_clarification.py`, replace
`test_shared_clarification_defers_technical_schema_questions` with:

```python
@pytest.mark.parametrize(
    "former_keyword",
    [
        "runtime",
        "catalog",
        "schema",
        "table",
        "column",
        "field match",
        "join key",
        "linkage field",
        "identifier",
        "foreign key",
    ],
)
def test_shared_clarification_does_not_classify_prose_from_keywords(
    monkeypatch: pytest.MonkeyPatch,
    former_keyword: str,
) -> None:
    payloads: list[dict[str, Any]] = []

    def fake_interrupt(payload: dict[str, Any]) -> dict[str, str]:
        payloads.append(payload)
        return {
            "action": "answer",
            "answer": OPTIONS[0]["label"],
            "_clarification_interrupt_id": "interrupt-1",
        }

    monkeypatch.setattr(
        "epi_agent.tool_packs.general.clarification.interrupt",
        fake_interrupt,
    )
    arguments = _arguments(
        reason=f"The scientific description uses {former_keyword} wording."
    )

    result = build_general_tool_registry().invoke(
        "general-request_clarification",
        arguments,
        context=None,  # type: ignore[arg-type]
    )

    assert payloads == [
        {
            "type": "agent_clarification",
            "question": arguments["question"],
            "reason": arguments["reason"],
            "options": OPTIONS,
        }
    ]
    assert result.message == f"Human clarification answer: {OPTIONS[0]['label']}"
```

- [ ] **Step 2: Run the regression test and confirm that the current keyword gate blocks it**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_epi_agent_clarification.py::test_shared_clarification_does_not_classify_prose_from_keywords
```

Expected: FAIL with `TECHNICAL_CLARIFICATION_DEFERRED` for the former keywords.

- [ ] **Step 3: Remove the technical keyword classifier and deferral branch**

In `epi_agent/tool_packs/general/clarification.py`, delete this constant:

```python
_TECHNICAL_CLARIFICATION_PATTERN = re.compile(
    r"\b(?:runtime|catalog|schema|table|column|field match|"
    r"join key|linkage field|identifier|foreign key)\b",
    re.IGNORECASE,
)
```

Delete the complete `_is_technical_clarification` function:

```python
def _is_technical_clarification(
    *,
    question: str,
    reason: str,
    options: list[dict[str, Any]],
) -> bool:
    text = " ".join(
        [
            question,
            reason,
            *(str(option.get("label") or "") for option in options),
        ]
    )
    return _TECHNICAL_CLARIFICATION_PATTERN.search(text) is not None
```

Delete this branch from `RequestClarificationTool.invoke`, leaving `response = interrupt(...)` immediately after the local `question`, `reason`, and `options` values are constructed:

```python
if _is_technical_clarification(
    question=question,
    reason=reason,
    options=options,
):
    raise ToolExecutionError(
        "TECHNICAL_CLARIFICATION_DEFERRED",
        (
            "Technical schema-resolution questions cannot be sent to "
            "the user. Continue investigating with the catalog and "
            "relationship tools; report a technical failure only after "
            "the permitted checks are exhausted."
        ),
        recoverable=True,
    )
```

Keep `import re` because `_AGENT_DELEGATION_OPTION_PATTERNS` still uses it.
Keep `Any` because the public invocation and option-handling types still use it.

- [ ] **Step 4: Run the complete shared clarification test module**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_epi_agent_clarification.py
```

Expected: PASS, including all ten former-keyword cases and the existing agent-delegation, cancel, answer, and validation cases.

- [ ] **Step 5: Verify that no technical prose classifier remains**

Run:

```bash
rg -n "_TECHNICAL_CLARIFICATION_PATTERN|_is_technical_clarification|TECHNICAL_CLARIFICATION_DEFERRED" epi_agent tests
```

Expected: no output and exit status 1.

- [ ] **Step 6: Commit the lexical-gate removal**

```bash
git add epi_agent/tool_packs/general/clarification.py tests/test_epi_agent_clarification.py
git commit -m "fix: remove lexical clarification blocking"
```

### Task 2: Encode the evidence-first semantic policy

**Files:**
- Modify: `tests/test_epi_agent_registry.py`
- Modify: `tests/test_multi_study_db_rag_tools.py`
- Modify: `epi_agent/agent.py`
- Modify: `epi_agent/db_rag/prompt.py`

**Interfaces:**
- Consumes: `GENERAL_CORE_INSTRUCTIONS: str` and `DB_RAG_SYSTEM_PROMPT: str`
- Produces: workflow-neutral evidence-first rules in `GENERAL_CORE_INSTRUCTIONS` and database-specific investigation ordering in `DB_RAG_SYSTEM_PROMPT`
- Preserves: `build_general_system_prompt(...) -> str`, registered tool names, study-routing rules, and all dataset review/extraction rules

- [ ] **Step 1: Add failing workflow-neutral prompt assertions**

Append to `tests/test_epi_agent_registry.py`:

```python
def test_general_prompt_requires_evidence_first_clarification() -> None:
    prompt = " ".join(GENERAL_SYSTEM_PROMPT.split())

    for required in (
        "Before asking any clarification, use applicable registered evidence tools",
        "Ask when human intent or knowledge is genuinely required",
        "Never guess merely to avoid clarification",
        "user input cannot resolve",
        "Do not repeat a clarification",
    ):
        assert required in prompt
```

- [ ] **Step 2: Add failing DB-RAG investigation-order assertions**

Append to `tests/test_multi_study_db_rag_tools.py`:

```python
def test_db_rag_prompt_requires_evidence_first_clarification_order() -> None:
    prompt = " ".join(DB_RAG_SYSTEM_PROMPT.split())

    for required in (
        "Before asking about database uncertainty",
        "search the runtime catalog",
        "inspect plausible tables",
        "check relationship paths",
        "the user could reasonably provide the missing information",
        "meaning of a user-provided column",
        "report the demonstrated technical limitation",
    ):
        assert required in prompt
    assert "technical failure rather than requesting a clarification" not in prompt
```

- [ ] **Step 3: Run both new prompt tests and confirm that the policy text is absent**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest -q \
  tests/test_epi_agent_registry.py::test_general_prompt_requires_evidence_first_clarification \
  tests/test_multi_study_db_rag_tools.py::test_db_rag_prompt_requires_evidence_first_clarification_order
```

Expected: two FAIL results because the evidence-first wording has not been added and the old absolute technical-failure sentence remains.

- [ ] **Step 4: Add the workflow-neutral clarification rules**

In `GENERAL_CORE_INSTRUCTIONS` in `epi_agent/agent.py`, insert the following block after the opening paragraph and before `Attachment rules:`:

```text
Clarification rules:
- Before asking any clarification, use applicable registered evidence tools
  when they can establish the answer.
- Ask when human intent or knowledge is genuinely required. Never guess merely
  to avoid clarification.
- If investigation demonstrates a limitation that user input cannot resolve,
  report the limitation.
- Do not repeat a clarification the user has answered or that subsequent
  evidence has resolved.

```

Do not remove the existing analysis-specific requirement to call
`general-request_clarification` alone with concrete options. It defines the
interrupt protocol, while the new block defines when asking is appropriate.

- [ ] **Step 5: Replace the absolute DB-RAG technical-failure wording with an evidence-first boundary**

In `epi_agent/db_rag/prompt.py`, replace the paragraph beginning
`Use relationship tools to resolve database linkage` and ending
`rather than requesting a clarification` with:

```text
Use relationship tools to resolve database linkage. Before asking about
database uncertainty, search the runtime catalog, inspect plausible tables,
and check relationship paths as applicable. A missing direct name does not
establish that the data are absent: broaden catalog searches and inspect all
evidence-supported candidates before asking or ending the run.
Ask only when human intent or knowledge is genuinely required and the user
could reasonably provide the missing information, such as the meaning of a
user-provided column. Never ask the user for internal schema identifiers,
table names, or join keys that an installed package is responsible for
defining. If the permitted checks demonstrate a technical limitation that
user input cannot resolve, report the demonstrated technical limitation
without guessing.
```

Retain the immediately following rules requiring scientific clarification to
use `general-request_clarification` rather than ordinary assistant text.

- [ ] **Step 6: Run the prompt contract tests**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest -q \
  tests/test_epi_agent_registry.py \
  tests/test_multi_study_db_rag_tools.py \
  tests/test_study_routing.py \
  tests/test_epi_agent_root_state.py
```

Expected: PASS. The general policy is present, DB-RAG retains exact study scope and relationship requirements, and semantic study routing remains unchanged.

- [ ] **Step 7: Commit the semantic policy**

```bash
git add epi_agent/agent.py epi_agent/db_rag/prompt.py tests/test_epi_agent_registry.py tests/test_multi_study_db_rag_tools.py
git commit -m "feat: require evidence-first clarification"
```

### Task 3: Verify unchanged contracts and real workflow behavior

**Files:**
- Verify only: `utils/review_interrupts.py`
- Verify only: `api/schemas.py`
- Verify only: `frontend/src/Clarification.tsx`
- Verify only: `scripts/e2e_agent_activity_timeline_real.py`
- Verify only: `scripts/smoke_full_overview_study_routing_real.py`

**Interfaces:**
- Consumes: unchanged `agent_clarification` interrupt and answer/cancel resume payloads
- Produces: verification evidence only; this task must not modify API or frontend contracts

- [ ] **Step 1: Run the focused backend regression suite**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest -q \
  tests/test_epi_agent_clarification.py \
  tests/test_epi_agent_registry.py \
  tests/test_multi_study_db_rag_tools.py \
  tests/test_study_routing.py \
  tests/test_review_interrupts.py \
  tests/test_api_review_contract.py \
  tests/test_api_runtime.py
```

Expected: PASS with no `TECHNICAL_CLARIFICATION_DEFERRED` references and no clarification payload changes.

- [ ] **Step 2: Run the unchanged clarification frontend tests and build**

Run:

```bash
cd frontend
npm test -- Clarification.test.tsx App.test.tsx
npm run build
```

Expected: Vitest PASS and a successful TypeScript/Vite build. The rendered options still contain the concrete model choices plus exactly one UI-provided `Let the agent decide` choice.

- [ ] **Step 3: Run the real database workflow smoke once**

From the repository root, run:

```bash
.venv/bin/python scripts/e2e_agent_activity_timeline_real.py \
  --timeout-seconds 300 \
  --artifact-dir /tmp/evidence-first-clarification-activity-smoke
```

Expected: `PASS agent activity timeline smoke` within five minutes. The supported database request searches the catalog and reaches dataset-plan review without an unexpected clarification. If it fails or times out, do not rerun automatically; preserve and report the printed diagnostics directory.

- [ ] **Step 4: Run the real semantic study-routing smoke once**

Run:

```bash
.venv/bin/python scripts/smoke_full_overview_study_routing_real.py \
  --timeout-seconds 300 \
  --artifact-dir /tmp/evidence-first-clarification-routing-smoke
```

Expected: `PASS full-overview study routing smoke` within five minutes. Unsupported routing remains evidence-based and invokes no study-dependent retrieval. If it fails or times out, do not rerun automatically; preserve and report the printed diagnostics directory.

- [ ] **Step 5: Confirm the final diff is scoped and clean**

Run:

```bash
git status --short
git diff --check HEAD~2..HEAD
git diff --stat HEAD~2..HEAD
```

Expected: no uncommitted files; no whitespace errors; only the two implementation commits affecting the six planned source/test files. Do not amend unrelated existing commits or clean unrelated user changes.
