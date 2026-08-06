# Study Design Document Folder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the rigid, single study-design JSON input with an optional Markdown document tree whose `overview.md` is always-on authoritative context and whose Markdown files are also retrievable through the existing study-knowledge Chroma collection.

**Architecture:** The Database repository owns Markdown validation, deterministic chunking, embedding, and release packaging. The Agent repository owns manifest compatibility, install-time contract validation, the runtime provider, prompt context, and a dedicated retrieval tool. Package format v3 carries the Markdown tree; format v2 remains loadable through the existing JSON provider.

**Tech Stack:** Python 3.12, Pydantic, DuckDB, ChromaDB, pytest, Typer/argparse-style CLIs already present in each repository.

## Global Constraints

- [ ] Execute repository changes in isolated git worktrees using `using-git-worktrees`; preserve unrelated changes in the current Epi-AI-Agent checkout.
- [ ] Keep `study_design/` entirely optional. A package without it installs and runs without study-design capability.
- [ ] When declared, require root `overview.md`: UTF-8, nonempty after trimming, and at most 32 KiB encoded bytes.
- [ ] Treat every other nested `.md` file as optional and retrieval-only. Include `overview.md` in retrieval as well.
- [ ] Package non-Markdown files but do not index them. Return warnings containing their exact relative paths; warnings never fail build or installation.
- [ ] Store design chunks in existing `study_knowledge` with `source_kind="study_design"`; preserve publication rows as `source_kind="publication"`.
- [ ] Make packaged Markdown paths and SHA-256 hashes exactly match indexed design sources so stale indexes cannot install.
- [ ] Preserve format-v2 archives using `study_design.document` and the legacy JSON provider.
- [ ] Keep the public `study_installer.py` arguments unchanged; add warning output only.
- [ ] Do not edit unrelated dirty files, especially `db_rag/vectorstore.py` and `api/app.py`; import existing interfaces from them if needed.
- [ ] Run the required real feature smoke once, within the five-minute policy, after focused tests pass.

---

### Task 1: Parse and Chunk Markdown Study Design Sources in Database

**Files:**

- Create: `Database/report-india-synthetic/scripts/study_design/__init__.py`
- Create: `Database/report-india-synthetic/scripts/study_design/index.py`
- Create: `Database/report-india-synthetic/tests/test_study_design_index.py`

- [ ] **Step 1: Write failing validation and discovery tests**

Cover: absent root returns an empty index; `overview.md` plus nested Markdown are discovered in sorted POSIX-path order; non-Markdown regular files become warnings; every Markdown file must be nonempty UTF-8; symlinks and special files are rejected; and missing, empty, invalid UTF-8, and over-32-KiB overview each raise an error whose text identifies the problem and both fixes (remove the study-design folder/declaration, or add/fix `overview.md`). Optional Markdown has no special file-size limit and is bounded through chunking.

```python
def test_absent_study_design_is_empty(tmp_path):
    result = build_study_design_index(None)
    assert result.sources == ()
    assert result.chunks == ()
    assert result.warnings == ()

def test_non_markdown_file_is_warning_not_failure(tmp_path):
    root = tmp_path / "study_design"
    root.mkdir()
    (root / "overview.md").write_text("# Overview\n\nAuthoritative.", encoding="utf-8")
    (root / "diagram.png").write_bytes(b"not-an-image")
    result = build_study_design_index(root)
    assert [warning.path for warning in result.warnings] == ["diagram.png"]
```

- [ ] **Step 2: Run the new tests and confirm failure**

Run from `Database/report-india-synthetic`:

```bash
.venv/bin/python -m pytest tests/test_study_design_index.py -q
```

Expected: FAIL during collection because `scripts.study_design.index` does not exist.

- [ ] **Step 3: Implement the source contract and deterministic chunker**

Define immutable dataclasses:

```python
@dataclass(frozen=True)
class StudyDesignWarning:
    path: str
    message: str

@dataclass(frozen=True)
class StudyDesignSource:
    path: str
    sha256: str

@dataclass(frozen=True)
class StudyDesignChunk:
    chunk_id: str
    document: str
    metadata: dict[str, str | int]

@dataclass(frozen=True)
class StudyDesignIndex:
    sources: tuple[StudyDesignSource, ...]
    chunks: tuple[StudyDesignChunk, ...]
    warnings: tuple[StudyDesignWarning, ...]

class StudyDesignInputError(ValueError): ...

def build_study_design_index(root: Path | None) -> StudyDesignIndex: ...
```

Use normalized relative POSIX paths, SHA-256 of original bytes, and deterministic heading-aware chunks with stable IDs derived from `path`, section heading, and ordinal. Each chunk metadata must contain `source_kind`, stable `source_id`, `source_path`, `source_sha256`, `section`, `body_text`, and `chunk_ordinal`; `source_kind` is always `study_design`. Each non-Markdown warning message must use `<relative-path> is not consumed by study-design indexing`.

- [ ] **Step 4: Add chunking assertions and pass the suite**

Assert headings do not disappear, long sections split deterministically, `overview.md` is included, and two runs produce identical IDs and metadata.

```bash
.venv/bin/python -m pytest tests/test_study_design_index.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the Database change**

```bash
git add scripts/study_design tests/test_study_design_index.py
git commit -m "feat: parse markdown study design sources"
```

---

### Task 2: Add Design Chunks to the Existing Database Index

**Files:**

- Modify: `Database/report-india-synthetic/scripts/database_index/build.py`
- Modify: `Database/report-india-synthetic/tests/test_database_index_builder.py`
- Modify: `Database/report-india-synthetic/scripts/validation/smoke_database_index_build.py`

- [ ] **Step 1: Write failing builder tests**

Extend the existing one-table fixture tests with:

```python
result = build_database(
    source_root=source_root,
    output_root=output_root,
    knowledge_root=publication_root,
    study_design_root=design_root,
    embedding_function=fake_embeddings,
)
```

Assert `study_knowledge` contains both source kinds, design rows expose the source path/hash metadata, publication rows remain unchanged, and non-Markdown paths raise `UserWarning` without failing. Retain the existing assertions of one table summary and one chunk per fixture column.

- [ ] **Step 2: Confirm the interface test fails**

```bash
.venv/bin/python -m pytest tests/test_database_index_builder.py -q
```

Expected: FAIL because `build_database()` does not accept `study_design_root`.

- [ ] **Step 3: Integrate the parser without creating new collections**

Add keyword-only `study_design_root: Path | None = None`, build design chunks, combine them with publication chunks, and write both to `study_knowledge`. Generalize the existing publication metadata construction so its `source_kind="publication"` remains explicit. Preserve the existing `Path` return value; emit each skipped-file message with `warnings.warn()` so library callers and the CLI both surface it without treating it as failure. Add CLI option `--study-design-root`; omission means no design.

- [ ] **Step 4: Pass focused and real-smoke unit coverage**

```bash
.venv/bin/python -m pytest tests/test_database_index_builder.py -q
```

Expected: PASS without network access; the real smoke itself is not run yet.

- [ ] **Step 5: Commit the Database change**

```bash
git add scripts/database_index/build.py scripts/validation/smoke_database_index_build.py tests/test_database_index_builder.py
git commit -m "feat: index markdown study design knowledge"
```

---

### Task 3: Publish Package Format v3 from Database

**Files:**

- Create: `Database/report-india-synthetic/study/study_design/overview.md`
- Create: `Database/report-india-synthetic/study/study_design/reference/population-aliases.md`
- Delete: `Database/report-india-synthetic/study/study_design/report.study-design.json`
- Modify: `Database/report-india-synthetic/scripts/package_release/build.py`
- Modify: `Database/report-india-synthetic/tests/test_release_builder.py`
- Modify: `Database/report-india-synthetic/tests/test_release_archive.py`
- Modify: `Database/report-india-synthetic/README.md`

- [ ] **Step 1: Write failing v3 release tests**

Test a database-only package, an overview-only design, nested optional Markdown, packaged non-Markdown assets, and rejection when packaged Markdown path/hash pairs differ from Chroma metadata. Assert the exact v3 manifest shape:

```json
{
  "format_version": 3,
  "study_design": {
    "root": "study-design",
    "overview": "overview.md"
  }
}
```

The `overview` field must be exactly `overview.md`. A package without design omits `study_design` and must not require a `study-design/` directory.

- [ ] **Step 2: Confirm the release tests fail**

```bash
.venv/bin/python -m pytest tests/test_release_builder.py tests/test_release_archive.py -q
```

Expected: FAIL because the builder still emits format v2 and accepts one JSON file.

- [ ] **Step 3: Replace the study source with authoritative Markdown**

Create `overview.md` containing the existing RePORT facts without inventing new design claims:

```markdown
# RePORT India Study Design

## Purpose
RePORT India investigates tuberculosis risk and outcomes in index cases and their household contacts.

## Study populations
- **Index cases (Cohort A):** participants with tuberculosis who anchor household recruitment.
- **Household contacts (Cohort B):** people recruited through their relationship to an index case.

## Relationship
Household contacts are linked to their corresponding index case through the study's household relationship.

## Authority
This overview is the authoritative default study-design context. Retrieved design documents may add detail, but this overview controls when they conflict unless it explicitly delegates authority or identifies a superseding amendment.
```

Create `reference/population-aliases.md` from the legacy JSON aliases so the canonical package and real smoke exercise nested retrieval-only content. It must state that Cohort A is also described as active pulmonary TB/index case and Cohort B as household contact/contact, without adding facts absent from the legacy source.

- [ ] **Step 4: Implement the v3 release builder**

Change the Python API to accept `study_design_root: Path | None`. Copy the full safe tree when present, validate `overview.md`, include non-Markdown regular files unchanged, and compare the complete set of packaged `.md` `(relative_path, sha256)` pairs with design-source metadata read from `study_knowledge`. Require a nonempty `study_knowledge` collection whenever either publication knowledge or design is present. Emit parser warnings. Update the CLI to `--study-design-root` and default new releases to version `0.3.0` while retaining the existing `0.2.0` archive as a compatibility fixture.

- [ ] **Step 5: Document and verify the release contract**

```bash
.venv/bin/python -m pytest tests/test_study_design_index.py tests/test_database_index_builder.py tests/test_release_builder.py tests/test_release_archive.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit the Database release change**

```bash
git add README.md scripts/package_release/build.py study/study_design tests/test_release_builder.py tests/test_release_archive.py
git commit -m "feat: publish version three study design packages"
```

---

### Task 4: Accept Both v2 and v3 Manifests in Agent

**Files:**

- Modify: `Epi-AI-Agent/study_package/manifest.py`
- Modify: `Epi-AI-Agent/tests/study_package_fixtures.py`
- Modify: `Epi-AI-Agent/tests/test_study_package_manifest.py`

- [ ] **Step 1: Write failing compatibility tests**

Keep existing v2 fixtures as the default compatibility case. Add v3 fixture support and tests that accept the exact Markdown manifest, reject `overview` values other than `overview.md`, reject a v2 manifest with the v3 shape and vice versa, and accept a v3 manifest with no `study_design` field.

```python
def test_v3_markdown_design_manifest_is_valid():
    manifest = StudyPackageManifest.model_validate({
        "format_version": 3,
        "study_design": {"root": "study-design", "overview": "overview.md"},
        # existing database and index declarations
    })
    assert manifest.study_design.root == "study-design"
```

- [ ] **Step 2: Confirm tests fail on format version 3**

Run from `Epi-AI-Agent`:

```bash
.venv/bin/python -m pytest tests/test_study_package_manifest.py -q
```

Expected: FAIL because `format_version` is currently restricted to 2.

- [ ] **Step 3: Model version-specific design declarations**

Introduce separate models:

```python
class LegacyStudyDesignManifest(BaseModel):
    document: str

class MarkdownStudyDesignManifest(BaseModel):
    root: str
    overview: Literal["overview.md"]
```

Allow `format_version: Literal[2, 3]`, then use a model validator to enforce the matching declaration shape. Apply the existing safe-relative-path validation to both new fields. Continue treating the v2 `document` as a file and treat the v3 `root` as a directory. Do not silently translate one shape into the other.

- [ ] **Step 4: Extend fixtures with indexed Markdown metadata**

Add a fixture option that writes `study-design/overview.md`, optional nested Markdown/non-Markdown files, and matching `study_knowledge` records with `source_kind="study_design"`, stable `source_id`, `source_path`, `source_sha256`, `section`, and `body_text`. Leave fixture defaults at v2 so legacy tests stay meaningful.

- [ ] **Step 5: Pass manifest tests and commit**

```bash
.venv/bin/python -m pytest tests/test_study_package_manifest.py -q
git add study_package/manifest.py tests/study_package_fixtures.py tests/test_study_package_manifest.py
git commit -m "feat: accept version three study manifests"
```

Expected: tests PASS before commit.

---

### Task 5: Validate and Load Markdown Designs During Installation

**Files:**

- Create: `Epi-AI-Agent/db_rag/study_design_documents.py`
- Modify: `Epi-AI-Agent/epi_agent/studies.py`
- Modify: `Epi-AI-Agent/db_rag/study.py`
- Modify: `Epi-AI-Agent/study_package/installer.py`
- Modify: `Epi-AI-Agent/study_installer.py`
- Modify: `Epi-AI-Agent/tests/test_study_package_installer.py`
- Modify: `Epi-AI-Agent/tests/test_study_installer_cli.py`
- Modify: `Epi-AI-Agent/tests/test_installed_study_bundle.py`
- Modify: `Epi-AI-Agent/tests/test_study_design.py`
- Create: `Epi-AI-Agent/tests/test_study_design_documents.py`

- [ ] **Step 1: Write failing installer contract tests**

Add cases for: no design directory; valid overview-only v3 design; nested Markdown; missing/empty/non-UTF-8/oversized overview; empty or non-UTF-8 optional Markdown with its exact path; exact Markdown path/hash mismatch; extra or missing Chroma design source; non-Markdown warning; and unchanged v2 JSON installation. In the provider test, use a recording Chroma collection to assert the exact `where={"source_kind": "study_design"}` query and hit provenance mapping without network access. Error assertions must include the offending path and actionable fixes:

1. Remove the `study_design` declaration/folder entirely.
2. Add or fix `overview.md` (and shorten it or move detail to retrieval documents when oversized).

Warnings must not set a failure exit code or use failure wording.

- [ ] **Step 2: Confirm focused installer tests fail**

```bash
.venv/bin/python -m pytest \
  tests/test_study_package_installer.py \
  tests/test_study_installer_cli.py \
  tests/test_installed_study_bundle.py \
  tests/test_study_design.py \
  tests/test_study_design_documents.py -q
```

Expected: FAIL because v3 directory declarations and warnings are unsupported.

- [ ] **Step 3: Add warning transport without changing CLI arguments**

Define:

```python
@dataclass(frozen=True)
class PackageWarning:
    path: str
    message: str
```

Carry `tuple[PackageWarning, ...]` on staged/installed results. Make validation return warnings. Print each as `Warning: <path>: <message>` from `study_installer.py`, followed by the existing successful install output. Keep all existing CLI flags and positional arguments.

- [ ] **Step 4: Implement v3 validation and exact index bijection**

For v3, enumerate all `.md` files below the declared root, validate every Markdown file as nonempty UTF-8, apply the 32-KiB limit specifically to `overview.md`, and compute SHA-256 from package bytes. Read unique `(source_path, source_sha256)` pairs from `study_knowledge` where `source_kind="study_design"`. Reject unless the two sets are exactly equal. Ignore publication rows for this comparison. Convert every packaged non-Markdown regular file into a warning while allowing installation. Its message must say `<relative-path> is not consumed by study-design indexing`; reject symlinks or other unsafe entries through the existing archive/package safety boundaries.

- [ ] **Step 5: Implement the Markdown runtime provider**

In `db_rag/study_design_documents.py`, implement:

```python
@dataclass(frozen=True)
class StudyDesignHit:
    source_kind: Literal["study_design"]
    source_id: str
    source_path: str
    source_sha256: str
    section: str
    text: str
    distance: float | None

class MarkdownStudyDesign:
    @classmethod
    def from_package(cls, package_root: Path, manifest: StudyPackageManifest) -> "MarkdownStudyDesign": ...
    def render_context(self) -> str: ...
    def search(self, query: str, limit: int = 5) -> tuple[StudyDesignHit, ...]: ...
```

`from_package()` resolves the declared design root and `manifest.database.index` through safe package paths and stores the study/package identity. `render_context()` returns the decoded `overview.md` with only outer whitespace stripped. `search()` loads the app environment, requires `OPENAI_API_KEY`, constructs the existing `OpenAIEmbeddingFunction` with `manifest.database.embedding_model`, lazily opens `study_knowledge` with that embedding function, and calls Chroma with `where={"source_kind": "study_design"}`. Do not edit `vectorstore.py`. Keep the legacy `LocalStudyDesign` implementation intact.

- [ ] **Step 6: Dispatch providers by manifest version**

Update `StudyDesignProvider` to require `render_context`; expose search through a separate searchable protocol or guarded capability so v2 providers remain valid. In `db_rag/study.py`, load legacy JSON for v2 and `MarkdownStudyDesign` for v3. A manifest with no design yields `study_design=None` and no tool capability.

- [ ] **Step 7: Pass focused tests and commit**

```bash
.venv/bin/python -m pytest \
  tests/test_study_package_manifest.py \
  tests/test_study_package_installer.py \
  tests/test_study_installer_cli.py \
  tests/test_installed_study_bundle.py \
  tests/test_study_design.py \
  tests/test_study_design_documents.py -q
git add db_rag/study_design_documents.py db_rag/study.py epi_agent/studies.py \
  study_installer.py study_package/installer.py \
  tests/study_package_fixtures.py tests/test_study_package_installer.py \
  tests/test_study_installer_cli.py tests/test_installed_study_bundle.py \
  tests/test_study_design.py tests/test_study_design_documents.py
git commit -m "feat: install markdown study design packages"
```

Expected: tests PASS before commit. Review `git diff --cached --name-only` before committing so unrelated tests or user changes are not staged.

---

### Task 6: Inject Overview Context and Add Dedicated Design Retrieval

**Files:**

- Create: `Epi-AI-Agent/epi_agent/tool_packs/study_design/__init__.py`
- Create: `Epi-AI-Agent/epi_agent/tool_packs/study_design/prompt.py`
- Create: `Epi-AI-Agent/epi_agent/tool_packs/study_design/tools.py`
- Modify: `Epi-AI-Agent/epi_agent/agent.py`
- Create: `Epi-AI-Agent/tests/test_study_design_tools.py`
- Modify: `Epi-AI-Agent/tests/test_epi_agent_root_state.py`

- [ ] **Step 1: Write failing context and tool tests**

Assert:

- overview Markdown is preserved as always-on context rather than collapsed to one line and is labeled with study/package identity;
- a searchable v3 provider registers `study-design-search`;
- no-design and legacy non-searchable providers do not register that tool;
- the tool sends only `source_kind="study_design"` hits and never publication hits;
- result text identifies stable source ID, source path, source hash, and section and is bounded;
- tool/prompt instructions state that overview controls conflicts unless it delegates or identifies a superseding amendment.

- [ ] **Step 2: Confirm the new behavior is absent**

```bash
.venv/bin/python -m pytest tests/test_study_design_tools.py tests/test_epi_agent_root_state.py -q
```

Expected: FAIL because there is no design tool pack and overview whitespace is collapsed.

- [ ] **Step 3: Implement the dedicated tool pack**

Use the existing tool-pack patterns. Define `study-design-search` with a query limited to 8,000 characters and `limit` constrained to 1–10. Call only the searchable design provider. Return a bounded artifact containing structured hits plus a readable summary with relative source paths and sections. The prompt must tell the agent to use retrieval for details not present in overview and to apply the authority rule.

- [ ] **Step 4: Register capability and preserve Markdown context**

Register the tool only when the provider supports search. In `epi_agent/agent.py`, replace the existing `" ".join(context.split())` normalization with `context.strip()` so headings and paragraphs survive, and wrap it with the active study ID and package version as authoritative context. Keep publication tool registration separate and unchanged.

- [ ] **Step 5: Pass focused tests and commit**

```bash
.venv/bin/python -m pytest tests/test_study_design_tools.py tests/test_epi_agent_root_state.py -q
git add epi_agent/agent.py epi_agent/tool_packs/study_design tests/test_study_design_tools.py tests/test_epi_agent_root_state.py
git commit -m "feat: expose study design context and retrieval"
```

Expected: tests PASS before commit.

---

### Task 7: Add and Run the Dedicated Real Feature Smoke

**Files:**

- Create: `Epi-AI-Agent/scripts/smoke_study_design_package_real.py`
- Create: `Epi-AI-Agent/tests/test_smoke_study_design_package_real.py`
- Modify: `Database/report-india-synthetic/scripts/validation/smoke_database_index_build.py`
- Create: `Database/report-india-synthetic/delivery/report-india-synthetic-0.3.0.tar.gz`
- Create: `Database/report-india-synthetic/delivery/report-india-synthetic-0.3.0.tar.gz.sha256`

- [ ] **Step 1: Write a failing smoke-script contract test**

Import the script's argument parser/helper without running external services. Assert it requires `--archive`, has a timeout-safe main entry point, and does not expose flags for fake embeddings, stub providers, or bypassed validation.

```bash
.venv/bin/python -m pytest tests/test_smoke_study_design_package_real.py -q
```

Expected: FAIL because the dedicated smoke script does not exist.

- [ ] **Step 2: Implement the real subsystem-boundary smoke**

The script must:

1. Accept a real release archive through `--archive`.
2. Install it through the public `study_installer.py` production boundary into a temporary study home.
3. Load the installed bundle through the normal runtime loader.
4. Assert `render_context()` contains the overview title and preserves a Markdown section boundary.
5. Run a real alias-focused design retrieval query and assert it returns `reference/population-aliases.md`; every hit must be `source_kind="study_design"` with stable source ID, source hash, and packaged source path.
6. Confirm publication knowledge remains present and separate.
7. Print one concise PASS summary and preserve traceback/output on failure.

Do not stub the installer, Chroma, embedding function, runtime provider, or credentials. Make the script executable.

- [ ] **Step 3: Pass the contract test**

```bash
.venv/bin/python -m pytest tests/test_smoke_study_design_package_real.py -q
```

Expected: PASS without running the real smoke.

- [ ] **Step 4: Rebuild canonical Database artifacts and v3 delivery archive**

Before the first command, obtain explicit user approval for the real embedding request because study text is sent to the configured external embedding provider. Then run from `Database/report-india-synthetic`:

```bash
.venv/bin/python scripts/database_index/build.py \
  --source-root raw_data/db_rag_source \
  --knowledge-root study/derived/publication_indexes \
  --study-design-root study/study_design \
  --output-root study/derived/database

.venv/bin/python scripts/package_release/build.py \
  --database-root study/derived/database \
  --knowledge-root study/derived/publication_indexes \
  --study-design-root study/study_design \
  --delivery-root delivery \
  --version 0.3.0
```

Expected: both commands report PASS; the archive and sidecar are generated. Check the archive with `tar -tzf` and verify it contains `study-design/overview.md`, the v3 manifest, and no legacy `study-design/design.json`.

- [ ] **Step 5: Run the Database real index smoke once**

Update `scripts/validation/smoke_database_index_build.py` to pass a temporary copy of `overview.md` and assert publication/design source kinds. Run it once only:

```bash
.venv/bin/python scripts/validation/smoke_database_index_build.py
```

Expected: `database index build smoke passed` within five minutes. If it fails or times out, do not rerun; preserve and report its traceback and output.

- [ ] **Step 6: Run the Agent dedicated real smoke once**

Run from `Epi-AI-Agent`:

```bash
.venv/bin/python scripts/smoke_study_design_package_real.py \
  --archive ../Database/report-india-synthetic/delivery/report-india-synthetic-0.3.0.tar.gz
```

Expected: one PASS summary within five minutes. If it fails or times out, do not rerun; preserve and report the archive path, traceback, installer output, and search output.

- [ ] **Step 7: Commit smoke and generated release artifacts separately**

In `Epi-AI-Agent`:

```bash
git add scripts/smoke_study_design_package_real.py tests/test_smoke_study_design_package_real.py
git commit -m "test: smoke markdown study design packages"
```

In `Database/report-india-synthetic`:

```bash
git add scripts/validation/smoke_database_index_build.py \
  delivery/report-india-synthetic-0.3.0.tar.gz \
  delivery/report-india-synthetic-0.3.0.tar.gz.sha256
git commit -m "build: add version 0.3.0 study package"
```

---

### Task 8: Run Full Regressions and Audit the Contract

**Files:**

- Verify only; modify the smallest owning file and add a regression test if a failure reveals a defect.

- [ ] **Step 1: Run the complete Database test suite**

From `Database/report-india-synthetic`:

```bash
.venv/bin/python -m pytest -q
```

Expected: PASS.

- [ ] **Step 2: Run the complete Agent test suite**

From `Epi-AI-Agent`:

```bash
.venv/bin/python -m pytest -q
```

Expected: PASS.

- [ ] **Step 3: Inspect dependency and worktree health**

In each repository:

```bash
.venv/bin/python -m pip check
git diff --check
git status --short
```

Expected: `pip check` reports no broken requirements; `git diff --check` is silent; status contains only intentional feature changes or pre-existing user changes, with no staged unrelated files.

- [ ] **Step 4: Audit the acceptance matrix**

Confirm test evidence exists for every row:

| Case | Expected result |
|---|---|
| No `study_design` declaration/folder | Valid install; no overview context or design tool |
| `overview.md` only | Valid install; always-on context and retrievable overview |
| Optional nested Markdown | Indexed and returned only through design retrieval |
| Optional non-Markdown file | Packaged, not indexed, exact-path warning, successful install |
| Missing/empty/non-UTF-8/oversized overview | Actionable build/install error with both fixes |
| Packaged/indexed path or SHA mismatch | Build/install rejection |
| Format-v2 JSON archive | Installs and loads through legacy provider |
| One database table and one reviewed JSON schema | Existing table/column Chroma behavior still passes |
| Publications plus design | One `study_knowledge` collection, distinct `source_kind` values, separate tools |
| Overview conflicts with retrieved design detail | Overview wins unless it delegates or marks a superseding amendment |

- [ ] **Step 5: Review commit scope and history**

```bash
git log --oneline --decorate -8
git diff HEAD~4..HEAD --stat
```

Run in each repository with the range adjusted to its feature commit count. Expected: small, task-oriented commits; no unrelated user files; no accidental credential, environment, or raw-data additions.

- [ ] **Step 6: Produce the implementation handoff**

Report focused and full test counts, the two one-shot smoke outcomes, archive checksum, warnings observed, compatibility status, and any pre-existing dirty files left untouched. Do not claim completion unless all required verification commands have current passing output.
