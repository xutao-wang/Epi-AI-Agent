# Master cleanup verification — November 25, 2026

The user approved verification and publication of the remaining master cleanup.

- Restored the committed frontend source manifest. It matches the existing compiled bundle and current frontend inputs. No frontend source or build input changed, and no frontend rebuild was needed.
- Bedrock display names omit discovery provider/scope annotations and known trailing Amazon Bedrock/AWS provider annotations. Meaningful parenthetical names such as `Lab Model (70B)` remain visible. Model IDs, provider targets and reasoning settings remain unchanged.
- Corrected the stale custom-endpoint test expectation to preserve the configured name without adding `(Standard)`. The committed production baseline already behaved this way.
- Added `/dist/` to ignored generated content.

## Verification

Eight new Bedrock display cases failed against the committed production baseline before restoring the pending label edit. Independent review then identified overly broad parenthetical stripping. Four additional cases reproduced the meaningful-name loss before narrowing that rule. The final focused suite passed **33 tests**:

```sh
.venv/bin/python -m pytest -q tests/test_model_runtime_profiles.py tests/test_model_reasoning_matrix_smoke.py tests/test_bedrock_sonnet55.py tests/test_api_root_projection.py
.venv/bin/python scripts/verify_working_demo_delivery.py
```

The dedicated `scripts/smoke_bedrock_model_labels_real.py` ran once and passed in **6.9 seconds**. It launched real FastAPI and the compiled frontend, used genuine configured Bedrock catalog discovery and matching Cohere study embeddings, checked all six available Bedrock option labels, selected Terra in the browser by its stable ID, and verified that a new real thread retained that exact ID and display label. No browser errors occurred. This presentation check submitted no inference request and used no provider or application stubs.

The smoke retains a hard deadline through cleanup and preserves raw runtime options, thread state, page HTML/text, screenshot and server logs. Artifacts are in `.master-cleanup-2026-11-25/real-label-smoke/` in the parent workspace. Original local files and failing regression logs are preserved privately alongside it. Independent read-only review approved the final source and smoke, independently confirming the 33 focused tests.

This cleanup is scoped to master publication. It does not change the earlier website rollout or the master-aws branch.
