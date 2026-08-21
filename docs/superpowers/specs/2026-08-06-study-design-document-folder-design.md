# Study Design Document Folder Design

**Status:** Approved in conversation on 2026-08-06

## Purpose

Replace the new-package study-design contract from one RePORT-specific structured
JSON document to an optional folder of free-form UTF-8 Markdown documents. The
design must remain independent of the number of database tables and reviewed
schema JSON inputs, work with a one-table/one-schema package, and preserve
installation of existing format-v2 study archives.

The Database repository owns source documents, chunking, embedding, and release
construction. The agent repository owns the installed-package contract,
validation, runtime loading, and agent-facing tools. The location of the study
installer inside the app does not make the Database source layout part of the
agent: the installer validates only versioned runtime artifacts.

## Decisions

- A study may omit study design entirely.
- A new-format package that declares study design contains a `study-design/`
  directory and must contain `overview.md` at its root.
- `overview.md` is the authoritative always-on study-design context and is also
  indexed for retrieval.
- Every other nested Markdown document is optional and retrieval-only.
- All Markdown documents are indexed in the existing Chroma
  `study_knowledge` collection with `source_kind="study_design"`.
- Publication chunks continue to use `source_kind="publication"`; publication
  and design evidence remain separate agent capabilities and tools.
- If retrieved design material conflicts with `overview.md`, the overview wins
  unless it explicitly delegates authority to or identifies a superseding
  document or amendment.
- Non-Markdown files do not fail the build or installation. They remain package
  files but are not injected or indexed, and their exact relative paths are
  reported as nonfatal warnings.
- Existing format-v2 packages with structured JSON design documents remain
  installable and load through the legacy provider.
- New document-folder packages use format version 3.

## Source And Release Layout

The Database source layout is:

```text
study/
  study_design/                 # optional
    overview.md                 # required when the folder is declared
    eligibility.md              # optional
    visits.md                   # optional
    amendments/
      2026-01.md                # optional
```

The new release archive layout is:

```text
study-package.json
database/
  study.duckdb
  schema_catalog.json
  index/
knowledge/
  reviewed/                     # optional existing publication indexes
study-design/                   # optional
  overview.md
  ...
```

For format version 3, the optional manifest entry is:

```json
{
  "study_design": {
    "root": "study-design",
    "overview": "overview.md"
  }
}
```

`overview` is required to be exactly `overview.md` in this format. Both paths
are safe package-relative paths. If the Database source folder is absent, the
release manifest omits `study_design` and the package has no study-design
capability.

Format version 2 retains its existing shape:

```json
{
  "study_design": {
    "document": "study-design/design.json"
  }
}
```

The package loader uses a discriminated format-version contract rather than
guessing semantics from file names.

## Database Build Flow

The Database builder accepts an optional study-design source root. When it is
absent, DuckDB, the schema catalog, and table/column Chroma collections build as
they do today.

When the folder is present, the builder:

1. Validates `overview.md` as nonempty UTF-8 Markdown no larger than 32 KiB in
   encoded bytes.
2. Recursively discovers `.md` files in deterministic relative-path order.
3. Validates every discovered Markdown file as nonempty UTF-8 text.
4. Parses Markdown headings and produces deterministic, bounded retrieval
   chunks. Oversized heading sections are split without changing source order.
5. Adds the chunks to `study_knowledge` with metadata containing at least:
   `source_kind`, stable source ID, relative path, heading, body text, and the
   source-file content hash.
6. Emits a warning for every non-Markdown regular file under the source root.
7. Builds the release archive with the complete safe directory tree, including
   warned non-Markdown files.

The release builder verifies a bijection between packaged Markdown files and
the design sources represented in Chroma: every packaged `.md` path and hash
must be indexed, and every indexed design source path and hash must exist in the
packaged folder. This prevents a release from combining an index built from one
design revision with documents from another revision.

There is no new design-specific size limit for optional Markdown files beyond
the existing archive/build safety boundaries. The chunker, rather than prompt
injection, bounds their retrieval representation.

The current paired Excel/JSON input convention remains a Database-builder
implementation detail. Reducing the study to one table and one reviewed schema
JSON produces one `table_summaries` record and one `column_chunks` record per
reviewed runtime column; it requires no agent or package-contract change.

## Installation And Validation

The public `study_installer.py` arguments remain unchanged. It continues to
install local `.tar.gz` archives and activate `study_id@version` targets.

The lower-level package manifest and installer gain format-v3 support. During
staging they validate safe paths, the required overview, UTF-8 Markdown, the
overview byte limit, and the presence of indexed study-design rows when study
design is declared. They also re-hash every packaged Markdown file and require
the path/hash set to match the design sources recorded in Chroma exactly. A
failed archive never becomes active.

Validation is performed twice by design:

- Database build/release validation prevents publishing an invalid artifact.
- Agent installation validation protects installations from a
  malformed, incomplete, or incorrectly transferred artifact.

Failures state the exact reason and provide actionable repair choices. For a
missing overview, the message is equivalent to:

```text
STUDY_DESIGN_OVERVIEW_MISSING: Package declares study_design at
"study-design", but "study-design/overview.md" is missing.

Fix one of:
1. Remove the study_design declaration and study-design folder to publish a
   package without study-design capability.
2. Add a nonempty UTF-8 study-design/overview.md file.
```

Empty, invalid-UTF-8, and oversized overview failures identify the condition
and recommend fixing/shortening the overview, moving details to optional
Markdown files, or omitting study design. Invalid optional `.md` files fail and
identify the exact path because their extension declares that they should be
consumed.

Non-Markdown files are not validation failures. The lower-level installer
returns structured warnings and the top-level CLI prints their relative paths
as `not consumed by study-design indexing`.

## Runtime And Agent Consumption

For a selected format-v3 study, the runtime creates a study-design provider
with two responsibilities:

- `render_context()` returns the validated `overview.md` as bounded
  authoritative context.
- `search()` queries the installed package's `study_knowledge` Chroma
  collection with `source_kind="study_design"`.

The context builder preserves Markdown headings, lists, and line breaks. It
does not collapse the overview into a single whitespace-normalized line. The
context is labeled as authoritative for the selected study and includes its
package identity.

A dedicated `study-design-search` agent tool exposes bounded design hits. Each
hit includes the relative source path, heading, excerpt, stable source ID, and
content hash. It cannot return publication rows because the query applies the
study-design source-kind filter.

The existing publication capability remains separate. Reviewed publication
indexes remain validated package inputs, and publication tools continue to
return publication evidence only. Publication Chroma rows retain
`source_kind="publication"`; this feature does not require redesigning the
publication ingestion schema.

When no study design is declared, the study bundle has no design provider, no
overview is added to the prompt, the design-search capability reports not
configured, and database querying continues normally.

For format-v2 packages, the current JSON provider and identity validation
remain available as a compatibility path. New Database releases do not produce
new v2 design JSON.

## Chroma And Database Independence

The runtime contract remains based on generated artifacts, not Database source
files:

- `study.duckdb` stores participant tables.
- `schema_catalog.json` is the exact structured map of runtime tables and
  reviewed columns.
- Chroma provides semantic retrieval through `table_summaries`,
  `column_chunks`, and optional `study_knowledge` rows.

The number of source tables and JSON files is not part of the installer or
agent contract. Database may later read CSV, Parquet, a single schema source,
or another maintainer format without app changes, provided it emits the same
versioned runtime artifacts. Changing collection names, required metadata, or
the schema-catalog structure would be a separate runtime-contract migration.

## Verification

Database-side tests cover:

- One table plus one reviewed schema JSON with no study design.
- Overview-only design input.
- Overview plus nested optional Markdown documents.
- Deterministic heading-aware chunks and required provenance metadata.
- Publication and design rows carrying distinct source kinds.
- Non-Markdown files producing warnings while the build succeeds.
- Every overview failure and its actionable error text.
- Invalid optional Markdown identifying its exact path.
- Format-v3 manifest and archive contents.

Agent-side tests cover:

- Existing format-v2 JSON-design archives continuing to install and load.
- Format-v3 packages with no study design.
- Valid format-v3 overview-only and multi-document packages.
- Installer warning propagation without rollback or failure.
- Missing, empty, invalid-UTF-8, and oversized overview rejection before
  activation.
- Packaged Markdown versus indexed source path/hash mismatch rejection.
- Always-on context preserving Markdown formatting and package identity.
- Design retrieval filtering `source_kind="study_design"`.
- Publication retrieval remaining isolated to publication evidence.
- Overview authority instructions and absence of capability when design is not
  configured.

The required feature smoke exercises real production boundaries: build a new
Database archive, install it through `study_installer.py`, load the installed
study, assert the overview context, and retrieve a nested optional design
passage from the real Chroma store. Any external embedding/query call follows
the repository's one-run, five-minute smoke rule and requires the applicable
data-export approval.

## Out Of Scope

- PDF, DOCX, image, or other non-Markdown study-design ingestion.
- Automatic summarization or generation of `overview.md`.
- Live editing or reindexing of an installed immutable package.
- Replacing DuckDB or Chroma for a future managed-cloud architecture.
- Changing the paired Excel/JSON Database source convention in this feature.
- Migrating or rewriting existing published format-v2 archives.

## Acceptance Criteria

The feature is complete when a Database package can omit study design or ship a
validated `study-design/overview.md` with any number of optional nested Markdown
documents; the overview appears in every selected-study agent context; design
search returns only indexed design passages with provenance; non-Markdown files
produce visible warnings without installation failure; existing v2 archives
still load; and the one-table/one-schema database path remains green.
