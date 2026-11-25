# Master label cleanup implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Verify and publish the existing master cleanup approved by the user.

**Architecture:** Keep the existing Bedrock-only label shortening. Correct the stale custom-endpoint test expectation and restore the already verified frontend manifest. Display changes must retain actual model IDs and reasoning settings. Preserve meaningful parenthetical model names while removing known trailing provider annotations.

**Tech Stack:** Python 3.12, pytest, real FastAPI, compiled React frontend, Playwright.

## Global constraints

- Work on the user's existing master cleanup; do not create another worktree.
- No frontend build inputs change, so restore the committed manifest and verify its actual sources and assets.
- Keep credentials, deployed website, and master-aws unchanged.
- Run the dedicated real smoke once, within 300 seconds. Preserve failures and do not retry automatically.

## Task 1: Verify the existing edits

- [x] Save the original four local files privately before editing.
- [x] Restore `frontend/dist/build-manifest.json` from HEAD and run `.venv/bin/python scripts/verify_working_demo_delivery.py`.
- [x] Correct `tests/test_model_runtime_profiles.py` to expect the unchanged custom label `Cluster Model`.
- [x] Add eight Bedrock display cases across discovered, operator, Mantle, plain labels and reasoning settings; retain meaningful parenthetical custom-endpoint labels.
- [x] Run the eight cases against the committed production module, observe eight failures, and restore the user's existing two-line edit immediately afterward.
- [x] Add four failing cases for meaningful Bedrock `(70B)` qualifiers, narrow the trailing parenthetical rule to known provider annotations, and rerun focused checks.
- [x] Run `.venv/bin/python -m pytest -q tests/test_model_runtime_profiles.py tests/test_model_reasoning_matrix_smoke.py tests/test_bedrock_sonnet55.py tests/test_api_root_projection.py`.

## Task 2: Verify delivery and publish

- [x] Run `scripts/smoke_bedrock_model_labels_real.py` once with the real installed Cohere-compatible study root. Check actual API labels, rendered options, selection by ID, persisted thread model identity, and browser errors. No inference request is needed to verify model-label presentation.
- [x] Receive independent read-only review of `.gitignore`, the label edit, tests, and dedicated smoke.
- [ ] Record results, stage only the approved files, and check `git diff --cached --check`.
- [ ] Commit and push master to origin; verify remote HEAD and a clean primary checkout. No deployment is included.
