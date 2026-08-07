# Conversation Lifecycle Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop failed or empty first turns from creating visible saved conversations, guarantee a non-placeholder automatic title without delaying graph execution, prevent stale sidebar refreshes from resurrecting deleted conversations, and safely remove the 27 confirmed historical orphan rows with an explicit one-time command.

**Architecture:** Add an internal `pending`/`ready` lifecycle to SQLite history records. Runtime thread creation remains in memory only; the first accepted submission inserts a hidden pending row, starts title generation and graph execution concurrently, then promotes the row only after the configured checkpointer proves that exact user message ID or turn hash is durable. Automatic title completion and a daemon timeout race through one compare-and-set operation, so generated, fallback, and manual titles have deterministic precedence. The frontend combines request ordering with a mutation generation and performs an ordered post-delete refresh. A separate dry-run maintenance command classifies exact untitled rows conservatively and deletes only revalidated candidates inside one write transaction.

**Tech Stack:** Python 3.12, FastAPI runtime, LangGraph SQLite checkpointer, `sqlite3`, `concurrent.futures`, structured Python logging, pytest, React 19, TypeScript, Vitest, Playwright, compiled Vite frontend.

## Global Constraints

- Work only on branch `aws-test`; check `git branch --show-current` before each implementation task and stop if it differs.
- Follow test-driven development for every behavior: write the focused failing test, run it and observe the intended failure, implement the minimum production change, then rerun the focused and affected regression tests.
- Keep model selection, temperature, top-p, reasoning effort, prompts, main graph checkpoint frequency, and workflow limits unchanged.
- Creating a thread, selecting a model, or staging an attachment must not create a history row. A first accepted submission may create only a hidden `pending` row.
- A row becomes `ready` only when a durable checkpoint for the owner-scoped thread contains the exact submitted `HumanMessage.id` or `_message_turn_hash(message)`.
- Title generation remains concurrent with graph execution. It must not block, cancel, or fail the main workflow.
- Automatic title precedence is: manual rename first; otherwise first successful compare-and-set among generated title, immediate error fallback, and timeout fallback. A late provider result cannot replace a fallback.
- Logs may contain a stable title-error category, owner-independent thread ID, and exception class. They must not contain user text, provider keys, prompts, or provider response bodies.
- Pending rows are internal and never returned from `GET /api/conversations`.
- Cleanup is dry-run by default, requires explicit database and runtime-root paths, preserves every ambiguous row, and never recursively deletes runtime storage.
- Do not execute cleanup with `--apply` until the stopped-server dry run prints exactly the expected 27 candidates and the user explicitly approves that candidate set.
- Use `.venv/bin/python` for Python commands. Run the dedicated real smoke at most once and keep it below five minutes.
- Every frontend source change requires `npm --prefix frontend run build`, followed by `.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest`.
- Preserve the user's unrelated untracked archives and all unrelated worktree changes.

## File Map

- `api/conversation_history.py`: migrate lifecycle schema; insert, promote, hide, and conditionally delete pending rows; provide deterministic fallback normalization and automatic-title compare-and-set.
- `api/runtime.py`: keep blank threads in memory, create pending history on first submission, verify the submitted turn in the checkpoint, coordinate failure cleanup, schedule title timeout/fallback, and emit safe structured logs.
- `tests/test_conversation_history.py`: prove migration, lifecycle transitions, list filtering, owner scoping, and title precedence.
- `tests/test_api_runtime.py`: prove delayed persistence, exact checkpoint verification, initial failure cleanup, concurrent title/checkpoint orderings, fallback behavior, manual rename protection, and owner isolation.
- `api/conversation_cleanup.py`: conservatively inspect history, checkpoint IDs, and known legacy/current artifact locations; apply exact row deletions transactionally.
- `scripts/cleanup_orphan_conversations.py`: expose the one-time dry-run/`--apply` CLI.
- `tests/test_conversation_cleanup.py`: prove dry-run, exact qualification, ambiguity preservation, transactional apply, idempotency, and malformed/locked-database refusal.
- `frontend/src/App.tsx`: add list-mutation invalidation, title-poll cancellation, optimistic deletion, and an ordered authoritative refresh.
- `frontend/src/App.test.tsx`: reproduce the stale title-poll response arriving after DELETE 204 and verify it cannot resurrect the conversation.
- `scripts/smoke_conversation_lifecycle_recovery_real.py`: exercise the production FastAPI server and compiled browser UI without provider stubs.
- `frontend/dist/**`: regenerate the tracked production bundle and build manifest.

---

### Task 1: Add the Hidden Pending/Ready History Lifecycle

**Files:**

- Modify: `tests/test_conversation_history.py`
- Modify: `api/conversation_history.py`

**Interfaces:**

- Consumes: the existing owner-scoped `conversation_history` table.
- Produces: `create_pending(owner_user_id, thread_id, model_name) -> tuple[ConversationSummary, bool]`, `promote_pending(owner_user_id, thread_id) -> bool`, `delete_pending(owner_user_id, thread_id) -> bool`, and ready-only `list(owner_user_id)`.
- Preserves: existing `create(...)` as a ready-row helper for migrations, tests, and already-durable records; public `ConversationSummary` remains unchanged and does not expose lifecycle.

- [ ] **Step 1: Add failing schema and lifecycle tests**

Add focused tests that establish these exact boundaries:

```python
def test_pending_rows_are_hidden_until_promoted(tmp_path: Path) -> None:
    store = ConversationHistoryStore(tmp_path / "memory.db")

    pending, inserted = store.create_pending(
        "user-a", "thread-a", model_name="gpt-5.6-terra"
    )

    assert inserted is True
    assert pending.title == "Untitled conversation"
    assert store.get("user-a", "thread-a") is not None
    assert store.list("user-a") == []
    assert store.promote_pending("user-a", "thread-a") is True
    assert [item.thread_id for item in store.list("user-a")] == ["thread-a"]
    assert store.promote_pending("user-a", "thread-a") is False


def test_delete_pending_never_deletes_a_ready_row(tmp_path: Path) -> None:
    store = ConversationHistoryStore(tmp_path / "memory.db")
    store.create("user-a", "ready", model_name="gpt-5.4")
    store.create_pending("user-a", "pending", model_name="gpt-5.4")

    assert store.delete_pending("user-a", "ready") is False
    assert store.delete_pending("user-a", "pending") is True
    assert store.get("user-a", "ready") is not None
```

Also add a migration test that creates the current pre-lifecycle owner-scoped table, opens `ConversationHistoryStore`, and asserts every existing row receives `lifecycle='ready'`. Extend owner-isolation coverage so one user's promote/delete operation cannot affect the same thread ID owned by another user.

- [ ] **Step 2: Run the lifecycle tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_conversation_history.py \
  -k 'pending or lifecycle or owner_scoped' -q
```

Expected: attribute failures for the missing pending lifecycle methods and a schema assertion failure because `lifecycle` does not exist.

- [ ] **Step 3: Migrate the table and implement conditional transitions**

Add a checked lifecycle column to newly created tables:

```sql
lifecycle TEXT NOT NULL DEFAULT 'ready'
    CHECK(lifecycle IN ('pending', 'ready'))
```

For an existing owner-scoped table, use `ALTER TABLE ... ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'ready' CHECK(...)`. Drop and recreate the existing `conversation_history_owner_activity_idx` so its key order becomes `(owner_user_id, lifecycle, archived_at, updated_at DESC)`; `CREATE INDEX IF NOT EXISTS` alone would leave the old definition unchanged.

Implement pending insertion so callers can distinguish the exact request that inserted it:

```python
def create_pending(
    self,
    owner_user_id: str,
    thread_id: str,
    *,
    model_name: str,
) -> tuple[ConversationSummary, bool]:
    now = self._now()
    with self._connect() as connection:
        result = connection.execute(
            """
            INSERT OR IGNORE INTO conversation_history
            (owner_user_id, thread_id, title, title_source, model_name,
             lifecycle, created_at, updated_at, last_opened_at)
            VALUES (?, ?, ?, 'automatic', ?, 'pending', ?, ?, ?)
            """,
            (owner_user_id, thread_id, _UNTITLED, model_name, now, now, now),
        )
    record = self.get(owner_user_id, thread_id)
    assert record is not None
    return record, result.rowcount == 1
```

Implement promotion and provisional deletion as conditional, owner-scoped updates:

```sql
UPDATE conversation_history
SET lifecycle = 'ready', updated_at = ?
WHERE owner_user_id = ? AND thread_id = ? AND lifecycle = 'pending'

DELETE FROM conversation_history
WHERE owner_user_id = ? AND thread_id = ? AND lifecycle = 'pending'
```

Keep `get(...)` usable internally for either lifecycle. Change ordinary `list(...)` to include `AND lifecycle = 'ready'`. Keep lifecycle out of `_summary(...)` and the public response schema.

- [ ] **Step 4: Run focused and full history tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_conversation_history.py -q
```

Expected: all history tests pass, including legacy-table migration and rollback behavior.

- [ ] **Step 5: Commit the lifecycle store**

```bash
git add api/conversation_history.py tests/test_conversation_history.py
git commit -m "fix: add pending conversation lifecycle"
```

---

### Task 2: Persist Only a Durably Accepted First Turn

**Files:**

- Modify: `tests/test_api_runtime.py`
- Modify: `api/runtime.py`

**Interfaces:**

- Consumes: Task 1 lifecycle methods, the submitted `HumanMessage.id`, `_message_turn_hash(message)`, `app.get_state(checkpoint_config, subgraphs=True)`, and `_projection_values(snapshot)`.
- Produces: private `_checkpoint_contains_user_turn(snapshot, message_id, turn_hash) -> bool`, `_accept_initial_turn(...)`, and `_reject_initial_turn(...)` helpers.
- Preserves: `ApiGraphRunner.start_background_from_factory(...)` callback timing; success still means `app.invoke(initial_payload, config)` returned, but the runtime callback performs the durability proof before promotion.

- [ ] **Step 1: Add failing delayed-persistence and exact-checkpoint tests**

Add tests proving all of the following:

1. `create_thread(identity)` returns an owned in-memory ID but creates no history row and nothing appears in `list_conversations`.
2. Staging an attachment on that thread still creates no history row.
3. The first submission inserts a pending row, but it remains absent from `list_conversations` while a blocking graph invocation is still running.
4. A returned invocation whose saved state contains the exact `HumanMessage.id` promotes the row.
5. A saved state containing only the exact stable turn hash also promotes the row.
6. A returned invocation with a different message ID/hash is treated as an error and deletes the pending row.
7. An initial invocation exception deletes the pending row while retaining the existing staged/bound attachment compensation behavior.
8. A later failure on an already-ready conversation never deletes that ready row.
9. Two owners using the same public thread ID cannot promote or delete each other's pending row.

Use a checkpointing fake only for these unit tests:

```python
class _InitialCheckpointGraph(_RuntimeFakeGraph):
    def __init__(self, *, checkpoint_payload: bool = True, error: Exception | None = None):
        super().__init__(SimpleNamespace(values={}, next=(), interrupts=[]))
        self.checkpoint_payload = checkpoint_payload
        self.error = error

    def invoke(self, payload: dict, _config: dict) -> None:
        if self.checkpoint_payload:
            self.snapshot = SimpleNamespace(values=payload, next=(), interrupts=[])
        if self.error is not None:
            raise self.error
```

For the missing-evidence case, replace both the message ID and `MetaKeys.LAST_USER_MESSAGE_HASH`/user event hash before the success callback reads the snapshot. Assert the runner ends in `error` and the row is absent.

- [ ] **Step 2: Run the focused runtime tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_api_runtime.py \
  -k 'blank_thread or pending_history or exact_initial_turn or missing_initial_checkpoint or failed_initial_invoke' -q
```

Expected: blank-thread assertions fail because `create_thread` eagerly inserts history, and the exact-checkpoint cases fail because no lifecycle transition exists.

- [ ] **Step 3: Keep blank threads owned in memory without eager history**

Remove `history_store.create(...)` from `create_thread`. Change `_require_owned_thread` to consult the owner-scoped in-memory cache first, then fall back to a persisted row:

```python
key = (identity.owner_user_id, thread_id)
with self._lock:
    existing = self._threads.get(key)
if existing is not None:
    return existing

record = self.history_store.get(identity.owner_user_id, thread_id) if self.history_store else None
if self.history_store is not None and record is None:
    raise KeyError(thread_id)
```

This lets create/upload/submit continue during the current process while ensuring an empty thread disappears after restart because it was never a saved conversation.

- [ ] **Step 4: Add the exact durable-turn verifier**

Implement a side-effect-free helper that checks both canonical evidence paths:

```python
def _checkpoint_contains_user_turn(
    snapshot: Any,
    *,
    message_id: str,
    turn_hash: str,
) -> bool:
    values = _projection_values(snapshot)
    for message in list(values.get("messages") or []):
        if isinstance(message, HumanMessage) and str(message.id or "") == message_id:
            return True
    meta = dict(values.get("meta") or {})
    if str(meta.get(MetaKeys.LAST_USER_MESSAGE_HASH) or "") == turn_hash:
        return True
    events = list(dict(values.get("artifacts") or {}).get("conversation_events") or [])
    return any(
        isinstance(event, dict)
        and event.get("type") == "user"
        and str(event.get("user_turn_hash") or "") == turn_hash
        for event in events
    )
```

The success callback must call `app.get_state` with the same owner-scoped checkpoint config used by `invoke`. If the helper returns false, conditionally delete the pending row, run the existing unbound-attachment rollback, and raise a stable internal `InitialTurnCheckpointError` so the runner reports an error rather than silently accepting an unrecoverable conversation.

- [ ] **Step 5: Insert pending state only around the accepted first submission**

Immediately before reserving the background run, call `create_pending(...)` only if no history row exists. Wrap synchronous payload-factory/start failures so only a pending row is conditionally removed. Wire callbacks as follows:

```python
on_initial_payload_error=lambda: self._reject_initial_turn(
    identity, thread_id, thread, manifests
),
on_initial_payload_success=lambda: self._accept_initial_turn(
    identity,
    thread_id,
    thread,
    message_id=str(message.id),
    turn_hash=self._message_turn_hash(message),
    manifests=manifests,
),
```

`_accept_initial_turn` verifies checkpoint evidence, commits attachment bindings, then conditionally promotes. `_reject_initial_turn` preserves the existing distinction between linked and unlinked attachments and calls `delete_pending`; it is harmless for a ready conversation. If `start_background_from_factory` returns false or raises before the background thread starts, remove only the row inserted by this request.

- [ ] **Step 6: Run affected runtime tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_api_runtime.py \
  -k 'create_thread or submit or initial or history or owner or attachment or concurrent_first' -q
```

Expected: all selected runtime tests pass. The blocking test shows no list item before the checkpoint callback and exactly one ready item after it.

- [ ] **Step 7: Commit durable first-turn persistence**

```bash
git add api/runtime.py tests/test_api_runtime.py
git commit -m "fix: save conversations after durable first turn"
```

---

### Task 3: Make Concurrent Titles Bounded, Observable, and Deterministic

**Files:**

- Modify: `tests/test_conversation_history.py`
- Modify: `tests/test_api_runtime.py`
- Modify: `api/conversation_history.py`
- Modify: `api/runtime.py`

**Interfaces:**

- Consumes: the existing bounded `_title_executor`, Task 1 pending row, first user text, and `threading.Timer`.
- Produces: `fallback_conversation_title(first_message) -> str`, `set_initial_automatic_title(...) -> bool`, `_schedule_title(...)`, and structured title events `conversation_title_failed` and `conversation_title_timed_out`.
- Timeout: a private `title_timeout_seconds: float = 120.0` runtime field matches the existing bounded sidebar title wait; tests inject a short value. A daemon timer persists fallback without waiting for a stuck provider future.

- [ ] **Step 1: Add failing fallback and compare-and-set store tests**

Add these normalization cases:

```python
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ('  "Analyze   diabetes outcomes."  ', "Analyze diabetes outcomes"),
        ("", "Attached data analysis"),
        ("   ", "Attached data analysis"),
    ],
)
def test_fallback_conversation_title_is_deterministic(message: str, expected: str) -> None:
    assert fallback_conversation_title(message) == expected
```

Also prove `set_initial_automatic_title` updates only an automatic exact `Untitled conversation`; it must return false after a fallback/generated title or manual rename.

- [ ] **Step 2: Add failing runtime races**

Replace the old “failure stays untitled” expectation with tests that prove:

- an immediate provider exception logs one safe `conversation_title_failed` event and stores the normalized first-message fallback;
- attachment-only input stores `Attached data analysis`;
- a blocking provider causes the daemon timeout to log `conversation_title_timed_out` and store fallback while graph execution continues;
- a provider result arriving after timeout cannot overwrite fallback;
- title completion before checkpoint promotion is retained on the hidden pending row and becomes visible after promotion;
- checkpoint promotion before title completion remains ready and receives the eventual title;
- a manual rename before a late result remains unchanged;
- captured logs do not contain the first-message text, fake API key, or exception message/provider body.

- [ ] **Step 3: Run title tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_conversation_history.py tests/test_api_runtime.py \
  -k 'fallback_conversation_title or initial_automatic_title or title_failure or title_timeout or title_before_checkpoint or checkpoint_before_title or late_automatic' -q
```

Expected: missing fallback/CAS APIs and the current failure test still observes permanent `Untitled conversation`.

- [ ] **Step 4: Implement deterministic fallback and title CAS**

Normalize fallback text by collapsing whitespace before applying existing title cleanup:

```python
def fallback_conversation_title(first_message: str) -> str:
    collapsed = " ".join(str(first_message or "").split())
    if not collapsed:
        return _ATTACHMENT_ONLY_TITLE
    try:
        return ConversationHistoryStore._title(collapsed)
    except ValueError:
        return _ATTACHMENT_ONLY_TITLE
```

Implement automatic persistence as one SQLite compare-and-set:

```sql
UPDATE conversation_history
SET title = ?, updated_at = ?
WHERE owner_user_id = ? AND thread_id = ?
  AND title_source = 'automatic'
  AND title = 'Untitled conversation'
```

This operation may update pending or ready rows, which safely handles either title/checkpoint ordering. A manual rename or timeout fallback wins against every later automatic result.

- [ ] **Step 5: Schedule one provider future and one daemon timeout**

Submit `title_generator.generate(text)` directly to the existing two-worker executor. Start a daemon `threading.Timer` for the configured title timeout. The future callback cancels the timer and either compare-and-sets the generated title or immediately logs/persists fallback. The timer logs/persists fallback if the future has not completed. Both paths call the same compare-and-set, so races are idempotent.

Use structured logging without exception text or prompt content:

```python
logger.warning(
    "conversation title generation failed",
    extra={
        "event": "conversation_title_failed",
        "thread_id": thread_id,
        "error_type": type(error).__name__,
    },
)
```

For timeout, emit `event="conversation_title_timed_out"` and no provider exception fields. If the row was removed after initial-turn failure, the compare-and-set simply returns false.

- [ ] **Step 6: Run title and lifecycle regression tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_conversation_history.py tests/test_api_runtime.py \
  -k 'title or pending or initial_turn or manual' -q
```

Expected: all selected tests pass; no ready conversation remains untitled because of provider error/timeout, and submission returns while title generation is blocked.

- [ ] **Step 7: Commit title recovery**

```bash
git add api/conversation_history.py api/runtime.py tests/test_conversation_history.py tests/test_api_runtime.py
git commit -m "fix: recover failed conversation titles"
```

---

### Task 4: Prevent Deleted Conversations from Reappearing in the Sidebar

**Files:**

- Modify: `frontend/src/App.test.tsx`
- Modify: `frontend/src/App.tsx`

**Interfaces:**

- Consumes: existing request sequencing, `apiClient.deleteConversation`, title polling, and saved-conversation state.
- Produces: `savedConversationsMutationRef`, mutation invalidation, title-poll cancellation for the deleted thread, optimistic local deletion, and one post-204 authoritative refresh.

- [ ] **Step 1: Add the failing DELETE-204 stale-response race test**

Construct deferred responses in this order:

1. initial list renders one `Untitled conversation`;
2. title polling begins a second list GET and holds its real response;
3. the user confirms delete and DELETE returns 204;
4. a third, post-delete list GET returns `items: []`;
5. the older polling GET resolves last with the deleted item.

Assert the delete request was issued, title polling stops, the post-delete GET occurs, and the deleted button never reappears after the stale response resolves. Also add a post-delete-refresh failure case: DELETE succeeds, local removal remains, and the displayed error describes only the list refresh failure.

- [ ] **Step 2: Run the focused frontend test and verify RED**

Run:

```bash
npm --prefix frontend test -- App.test.tsx \
  -t 'does not restore a deleted conversation from stale title polling|keeps a confirmed deletion when refresh fails'
```

Expected: the old polling response can restore the item, and no ordered post-delete refresh exists.

- [ ] **Step 3: Add a mutation generation alongside request ordering**

Capture both values at refresh start and reject either kind of staleness:

```tsx
const savedConversationsMutationRef = useRef(0);

async function refreshSavedConversations(
  { surfaceError = false }: { surfaceError?: boolean } = {},
): Promise<ConversationSummary[] | null> {
  const requestId = ++savedConversationsRequestRef.current;
  const mutationGeneration = savedConversationsMutationRef.current;
  try {
    const response = await apiClient.listConversations();
    if (
      requestId !== savedConversationsRequestRef.current ||
      mutationGeneration !== savedConversationsMutationRef.current
    ) {
      return null;
    }
    const items = response.items ?? [];
    setSavedConversations(items);
    return items;
  } catch (refreshError) {
    if (surfaceError) setError(errorMessage(refreshError));
    return null;
  }
}
```

Create a small invalidator used when archive, restore, or delete starts:

```tsx
function invalidateSavedConversationRequests() {
  savedConversationsMutationRef.current += 1;
  savedConversationsRequestRef.current += 1;
}
```

- [ ] **Step 4: Make deletion locally final and remotely authoritative**

At delete start, invalidate older refreshes and clear `titlePollingThreadId` only when it matches the target. After DELETE 204, filter the item locally and reset the active conversation if necessary. Then call `refreshSavedConversations({ surfaceError: true })`. Do not put that refresh inside the DELETE failure branch; a refresh error must never make the UI claim deletion failed or restore the item.

Apply the same invalidation boundary at archive and restore start so older polling/list responses cannot reverse those mutations. If those mutations fail, run a normal refresh to reconcile.

- [ ] **Step 5: Run focused and full frontend tests and verify GREEN**

Run:

```bash
npm --prefix frontend test -- App.test.tsx \
  -t 'deleted conversation|saved-conversation list|title refresh|archive|restore'
npm --prefix frontend test
```

Expected: both commands pass; the deferred pre-delete response is ignored even when it resolves last.

- [ ] **Step 6: Build the production frontend**

Run:

```bash
npm --prefix frontend run build
```

Expected: TypeScript and Vite build successfully. Do not refresh the delivery manifest until the final tracked bundle is stable in Task 7.

- [ ] **Step 7: Commit the frontend consistency fix**

```bash
git add frontend/src/App.tsx frontend/src/App.test.tsx frontend/dist
git commit -m "fix: invalidate stale conversation refreshes"
```

---

### Task 5: Add the Conservative One-Time Orphan Cleanup Command

**Files:**

- Create: `api/conversation_cleanup.py`
- Create: `tests/test_conversation_cleanup.py`
- Create: `scripts/cleanup_orphan_conversations.py`

**Interfaces:**

- Consumes: one explicitly resolved SQLite database, one explicitly resolved runtime root, `conversation_history`, LangGraph `checkpoints`, the legacy checkpoint ID, `graph_config(..., owner_user_id=...)`, and known current/legacy artifact locations.
- Produces: immutable `CleanupCandidate`/`CleanupReport`, `inspect_orphan_conversations(...)`, `apply_orphan_cleanup(...)`, and a CLI that is dry-run unless `--apply` is passed.
- Output: one line per candidate/skip containing owner scope, thread ID, creation time, and classification evidence, followed by counts and an explicit `DRY RUN` or `APPLIED` summary.

- [ ] **Step 1: Add failing classifier and CLI tests**

Build temporary combined SQLite/runtime fixtures and prove:

- dry-run reports an exact untitled row with zero legacy/scoped checkpoints and zero artifacts but does not delete it;
- `--apply` deletes that exact row and a second apply is a zero-change success;
- a custom title is preserved;
- any checkpoint row under the public legacy thread ID is preserved;
- any checkpoint row under the owner-hashed ID is preserved;
- current owner-scoped files under `users/{owner_hash}/threads/{thread_id}` are preserved;
- legacy files under `runtime/{thread_id}`, `runtime/datasets/{thread_id}`, and `runtime/attachments/{sha256(thread_id)}` are preserved;
- an unowned row, malformed schema, unreadable path, symlink/path ambiguity, or SQLite lock exits nonzero without partial deletion;
- apply revalidates candidates after acquiring the transaction rather than deleting a stale dry-run list.

The CLI tests must invoke:

```bash
.venv/bin/python scripts/cleanup_orphan_conversations.py \
  --database /absolute/path/to/agent_memory_fastapi.db \
  --runtime-root /absolute/path/to/runtime
```

and the same command with `--apply`.

- [ ] **Step 2: Run cleanup tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_conversation_cleanup.py -q
```

Expected: import/subprocess failure because the classifier and command do not exist.

- [ ] **Step 3: Implement conservative checkpoint and artifact evidence**

Require the core history columns but tolerate the pre-lifecycle database so this command can clean the current file before application startup migrates it. For each exact untitled row, derive checkpoint IDs:

```python
legacy_checkpoint_id = thread_id
scoped_checkpoint_id = graph_config(
    thread_id,
    owner_user_id=owner_user_id,
)["configurable"]["thread_id"]
```

Any row in `checkpoints` for either ID is preservation evidence. Zero checkpoint rows is required for candidacy; this is intentionally stricter than trying to interpret partial or unknown checkpoint blobs.

Inspect only exact, resolved paths and treat any inspection error as ambiguous:

```python
current = UserStorageLayout(runtime_root).thread(owner_user_id, thread_id).root
legacy_direct = runtime_root / thread_id
legacy_dataset = runtime_root / "datasets" / thread_id
legacy_attachment = runtime_root / "attachments" / sha256(thread_id.encode()).hexdigest()
```

Also check exact legacy category paths for `figures`, `tables`, `exports`, and `execution`. A file or symlink anywhere under one of these exact roots preserves the row. Empty compatibility directories are not artifacts. Never glob by partial thread ID and never delete any path.

- [ ] **Step 4: Implement dry-run and transactional apply**

Dry-run opens read-only and prints the report. Apply uses `timeout=0`, `PRAGMA busy_timeout=0`, and `BEGIN IMMEDIATE`; after the lock is acquired it reruns the full classifier and deletes with all identity predicates:

```sql
DELETE FROM conversation_history
WHERE owner_user_id IS ?
  AND thread_id = ?
  AND title = 'Untitled conversation'
```

If lifecycle exists, also require `lifecycle = 'ready'` so the maintenance tool never races or removes a live pending submission. Verify each expected `rowcount == 1`; otherwise roll back the entire transaction. Print `APPLIED count=N` only after commit. The script description and help text must state: stop FastAPI first; dry-run is default; `--apply` is destructive only for printed/revalidated candidates.

- [ ] **Step 5: Run cleanup and adjacent persistence tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_conversation_cleanup.py tests/test_conversation_history.py -q
```

Expected: all tests pass, including idempotent second apply and locked-database refusal.

- [ ] **Step 6: Commit the maintenance command**

Because new files under `tests/` and `scripts/` are ignored, force-add only the two named files:

```bash
git add api/conversation_cleanup.py
git add -f tests/test_conversation_cleanup.py scripts/cleanup_orphan_conversations.py
git commit -m "fix: add orphan conversation cleanup command"
```

Do not run this command against `runtime/agent_memory_fastapi.db --apply` in this task.

---

### Task 6: Add and Run the Dedicated Real Lifecycle Smoke

**Files:**

- Create: `scripts/smoke_conversation_lifecycle_recovery_real.py`
- Modify: `tests/test_e2e_working_demo_native_real.py`

**Interfaces:**

- Consumes: the production `run_fastapi.py` launcher, compiled `frontend/dist`, a temporary runtime database, the real configured provider/model, and Playwright Chromium.
- Produces: one under-five-minute executable acceptance smoke with retained API log, database/list snapshots, page text, and screenshots on failure.

- [ ] **Step 1: Add a failing smoke-contract test**

Extend the cheap harness test to assert the new script:

- builds/serves the production UI rather than Vite dev mode;
- launches `run_fastapi.py` with a temporary `REPORT_AGENT_RUNTIME_ROOT` and `REPORT_AGENT_CHECKPOINT_DB_PATH`;
- does not import or inject fake graph/title providers;
- checks the raw conversation API and SQLite lifecycle column;
- uses a held real `/api/conversations` response only to reproduce network ordering for deletion;
- has a default timeout no greater than 300 seconds and always terminates the server.

- [ ] **Step 2: Run the contract test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_e2e_working_demo_native_real.py \
  -k conversation_lifecycle_recovery -q
```

Expected: failure because the feature smoke does not exist.

- [ ] **Step 3: Implement the real smoke**

The smoke must perform this exact sequence:

1. build the compiled frontend before starting the timer-sensitive browser flow;
2. start the real FastAPI app against an empty temporary runtime root;
3. call `POST /api/threads` and verify raw `GET /api/conversations` remains empty, including after model selection or an optional staged upload;
4. submit one real first turn through the browser;
5. while the graph is running, assert SQLite contains at most one `pending` row and the public list never returns it;
6. wait for the exact first user turn to become durable, then assert one `ready` public record appears with either a generated title or deterministic fallback, never `Untitled conversation`;
7. begin/hold one real list response used by title polling, delete the saved conversation through the UI, allow the ordered post-delete list to return empty, then release the older response;
8. verify the sidebar remains empty, reload, and verify raw API plus SQLite contain no row for the thread;
9. capture diagnostics and terminate the server in `finally`.

The held response may delay and later fulfill the actual server response; it must not synthesize replacement API data or stub the provider.

- [ ] **Step 4: Run cheap smoke-contract tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_e2e_working_demo_native_real.py \
  -k conversation_lifecycle_recovery -q
```

Expected: the structural tests pass without calling the provider.

- [ ] **Step 5: Run the dedicated real smoke exactly once**

Run once only:

```bash
.venv/bin/python scripts/smoke_conversation_lifecycle_recovery_real.py \
  --timeout-seconds 300
```

Expected: `PASS conversation lifecycle recovery smoke` and an artifact-directory path. If it fails, preserve and report diagnostics; do not rerun automatically.

- [ ] **Step 6: Commit the smoke**

```bash
git add -f tests/test_e2e_working_demo_native_real.py \
  scripts/smoke_conversation_lifecycle_recovery_real.py
git commit -m "test: smoke conversation lifecycle recovery"
```

---

### Task 7: Rebuild, Verify, and Prepare the One-Time Cleanup

**Files:**

- Modify: `frontend/dist/**`
- Modify: `frontend/dist/build-manifest.json`
- Inspect only: `runtime/agent_memory_fastapi.db` during dry-run

**Interfaces:**

- Consumes: all completed tasks and the repository delivery verifier.
- Produces: final compiled assets, manifest, verification evidence, and the printed candidate set for separate user approval.

- [ ] **Step 1: Run backend regression suites**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_conversation_history.py \
  tests/test_conversation_cleanup.py \
  tests/test_api_runtime.py \
  tests/test_api_server.py \
  tests/test_api_multi_user_isolation.py -q
```

Expected: all selected backend tests pass.

- [ ] **Step 2: Run the full frontend suite and production build**

Run:

```bash
npm --prefix frontend test
npm --prefix frontend run build
```

Expected: all Vitest tests pass and Vite emits a successful production build.

- [ ] **Step 3: Refresh and verify the delivery manifest**

Run:

```bash
.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest
.venv/bin/python scripts/verify_working_demo_delivery.py
```

Expected: the manifest is regenerated from the current tracked build and verification passes.

- [ ] **Step 4: Inspect diff and run final focused checks**

Run:

```bash
git diff --check
git status --short
```

Confirm no prompt/provider-key logging, no model sampling changes, no runtime database changes, and no unrelated archive files are staged.

- [ ] **Step 5: Commit final generated delivery assets if changed**

```bash
git add frontend/dist
git commit -m "build: refresh conversation lifecycle frontend"
```

Skip the commit if the preceding frontend task already committed identical generated assets.

- [ ] **Step 6: Stop the application server and run cleanup dry-run only**

After confirming no FastAPI process is using the runtime database, run:

```bash
.venv/bin/python scripts/cleanup_orphan_conversations.py \
  --database runtime/agent_memory_fastapi.db \
  --runtime-root runtime
```

Expected from the already-inspected database: `candidates=27`, with every candidate showing exact untitled title, zero checkpoint rows under legacy and scoped IDs, and zero artifact paths. The command must make no database change.

- [ ] **Step 7: Present the 27 candidate lines and request destructive approval**

Stop here. Give the user the complete dry-run summary and ask for explicit approval before running:

```bash
.venv/bin/python scripts/cleanup_orphan_conversations.py \
  --database runtime/agent_memory_fastapi.db \
  --runtime-root runtime \
  --apply
```

After approval, run the apply command once, rerun dry-run to confirm `candidates=0`, and report the applied count and recoverability: deleted history rows are not recoverable from the application unless the user has a database backup; no checkpoints or artifact files were removed.

- [ ] **Step 8: Request code review before integration**

Use the `requesting-code-review` skill to inspect lifecycle races, owner scoping, cleanup conservatism, title-log secrecy, and frontend request ordering. Address any verified high-confidence issues, rerun affected tests, and only then hand off merge/PR options.
