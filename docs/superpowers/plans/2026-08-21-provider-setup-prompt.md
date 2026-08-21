# Provider Setup Prompt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clarify hidden API-key submission and remove the combined provider-configuration menu action.

**Architecture:** Keep the existing provider setup loop and credential verifier intact. Change only its displayed menu choices, selection mapping, and provider-specific key prompt, with exact-string tests covering both first-run and reconfiguration paths.

**Tech Stack:** Python 3.12, `getpass`, pytest

## Global Constraints

- Use `Paste your <Provider> API key and press Enter to validate` followed by `(empty + Enter returns to provider setup):` for OpenAI and Anthropic.
- Configure OpenAI and Anthropic independently; do not offer `Configure both`.
- The application remains runnable when at least one provider verifies.
- Reconfiguration preserves existing provider keys unless the user selects the corresponding remove action.
- Do not change compatible-endpoint behavior or credential validation.

---

### Task 1: Clarify and simplify native provider setup

**Files:**
- Modify: `tests/test_run_fastapi.py`
- Modify: `run_fastapi.py`
- Modify: `README.md`
- Modify: `docs/working-demo.md`

**Interfaces:**
- Consumes: `configure_and_verify_providers(..., input_fn, getpass_fn, verifier, persist, force)`
- Produces: unchanged `ModelAvailability`; only interactive copy and menu selection numbers change.

- [ ] **Step 1: Write failing prompt and menu tests**

Add first-run coverage that captures both prompts and configures OpenAI independently:

```python
def test_first_run_menu_and_key_prompt_are_unambiguous(tmp_path: Path) -> None:
    menu_prompts: list[str] = []
    key_prompts: list[str] = []

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ={},
        input_fn=lambda prompt: menu_prompts.append(prompt) or "1",
        getpass_fn=lambda prompt: key_prompts.append(prompt) or "openai-key",
        verifier=lambda _provider, _key, **_kwargs: None,
        persist=lambda _root, _values: None,
        output_fn=lambda _message: None,
    )

    assert menu_prompts == [
        "No AI provider is configured.\n\n"
        "1. Configure OpenAI\n"
        "2. Configure Anthropic\n"
        "3. Connect to a compatible endpoint\n"
        "Selection: "
    ]
    assert key_prompts == [
        "Paste your OpenAI API key and press Enter to validate\n"
        "(empty + Enter returns to provider setup): "
    ]
    assert "gpt-5.6-terra" in catalog.available_model_ids
```

Add reconfiguration coverage using option 6 as the new keep action:

```python
def test_reconfigure_menu_has_independent_provider_actions(tmp_path: Path) -> None:
    prompts: list[str] = []

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ={"OPENAI_API_KEY": "openai-key"},
        input_fn=lambda prompt: prompts.append(prompt) or "6",
        getpass_fn=lambda _prompt: pytest.fail("key prompt should not open"),
        verifier=lambda _provider, _key, **_kwargs: None,
        persist=lambda _root, _values: None,
        output_fn=lambda _message: None,
        force=True,
    )

    assert prompts == [
        "Configure AI providers. Existing providers are retained.\n\n"
        "1. Configure or replace OpenAI\n"
        "2. Configure or replace Anthropic\n"
        "3. Connect to a compatible endpoint\n"
        "4. Remove OpenAI\n"
        "5. Remove Anthropic\n"
        "6. Keep current providers\n"
        "Selection [6]: "
    ]
    assert "gpt-5.6-terra" in catalog.available_model_ids
```

Replace `test_no_keys_can_configure_both_and_persist_only_verified_keys` with:

```python
@pytest.mark.parametrize(
    ("selection", "key", "expected_model", "expected_saved"),
    [
        (
            "1",
            "openai-key",
            "gpt-5.6-terra",
            {"OPENAI_API_KEY": "openai-key"},
        ),
        (
            "2",
            "anthropic-key",
            "claude-opus-5",
            {"ANTHROPIC_API_KEY": "anthropic-key"},
        ),
    ],
)
def test_no_keys_can_configure_one_provider_independently(
    tmp_path: Path,
    selection: str,
    key: str,
    expected_model: str,
    expected_saved: dict[str, str],
) -> None:
    saved: list[dict[str, str]] = []

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ={},
        input_fn=lambda _prompt: selection,
        getpass_fn=lambda _prompt: key,
        verifier=lambda _provider, _key, **_kwargs: None,
        persist=lambda _root, values: saved.append(values),
        output_fn=lambda _message: None,
    )

    assert saved == [expected_saved]
    assert expected_model in catalog.available_model_ids
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
PYTHONPATH=. ../../.venv/bin/pytest -q \
  tests/test_run_fastapi.py::test_first_run_menu_and_key_prompt_are_unambiguous \
  tests/test_run_fastapi.py::test_reconfigure_menu_has_independent_provider_actions
```

Expected: FAIL because the old menu contains `Configure both`, compatible endpoints use option 4, keep uses option 7, and the key prompt has the old wording.

- [ ] **Step 3: Implement the minimal menu and prompt changes**

In `_prompt_verified_builtin_key`, use:

```python
getpass_fn(
    f"Paste your {label} API key and press Enter to validate\n"
    "(empty + Enter returns to provider setup): "
)
```

In `_configure_provider_menu`, change first-run options to 1–3 and reconfiguration options to 1–6. Keep only these built-in mappings:

```python
providers = {
    "1": ("openai",),
    "2": ("anthropic",),
}.get(selection)
```

Handle compatible endpoints with selection `"3"`, removal with `{"4", "5"}`, and keep with `"6"`. Use default `"6"` during reconfiguration.

- [ ] **Step 4: Update setup documentation**

In `README.md` and `docs/working-demo.md`, remove wording that advertises a combined `both` option. State that OpenAI and Anthropic can both be enabled by configuring them independently through `--reconfigure`.

- [ ] **Step 5: Run focused and full verification**

Run:

```bash
PYTHONPATH=. ../../.venv/bin/pytest -q tests/test_run_fastapi.py
PYTHONPATH=. OPENAI_API_KEY=test-key ../../.venv/bin/pytest -q
```

Expected: all tests pass with the existing single skip allowed.

Run:

```bash
rg -n "Configure both|Configure OpenAI, Anthropic, both|offers OpenAI, Anthropic, both" \
  run_fastapi.py README.md docs/working-demo.md tests/test_run_fastapi.py
git diff --check
```

Expected: `rg` finds no stale combined-menu wording and `git diff --check` exits successfully.

- [ ] **Step 6: Commit the implementation**

```bash
git add run_fastapi.py README.md
git add -f tests/test_run_fastapi.py docs/working-demo.md
git commit -m "fix: clarify provider setup choices"
```
