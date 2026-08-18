# Agent-Driven Multi-Study Selection Design

## Goal

Allow one EpiAgent conversation to use any installed study package without a
user-operated selector, a thread-wide active study, or a mandatory routing
workflow. The agent will choose a study as part of each study-dependent
retrieval call. When the request does not identify a study clearly, the agent
may inspect bounded authoritative study overviews and either proceed with one
clear match or ask the user for clarification.

The implementation must support an arbitrary installed `StudyRegistry`, not a
hardcoded RePORT India and NHANES pair. RePORT India and NHANES 2017-2018 are
the initial real packages used for verification.

## Chosen architecture

Study identity is scoped per retrieval and then propagated through structured
references and immutable artifact provenance:

```text
minimal installed-study directory in agent context
        |
        +-- explicit study in request -----------------------+
        |                                                    |
        +-- unclear study -> optional search_studies() ------+
                                                             v
                                      search_catalog(study_id, queries)
                                                             |
                                          study-scoped schema references
                                                             |
                           inspect table / find joins / profile relationships
                                                             |
                                      one-study dataset plan and provenance
                                                             |
                                  validate / extract / inspect dataset
```

This replaces the current `ToolContext.study` selection boundary with an
authorized registry available to tool handlers. It does not create another
pre-routing graph node or a `study-select_for_turn` operation.

The following alternatives are rejected:

1. A thread-wide active study, because a later question in the same thread may
   clearly concern another study.
2. A mandatory router before every retrieval, because it constrains the
   agent's ordinary tool choice and repeats work when the user already named a
   study.
3. A structured routing-profile JSON, because fixed fields can omit or
   distort important differences among surveys, cohorts, registries, and
   future study types.
4. A `study_ids` list on `search_catalog`, because it combines independent
   failures and evidence, weakens provenance boundaries, and can multiply one
   bounded five-probe response into an oversized model observation.

## Installed-study context

Every agent turn receives only a compact directory containing each authorized
study's exact ID and human-readable label:

```text
Available installed studies:
- nhanes-2017-2018 — NHANES 2017-2018
- report-india-synthetic — RePORT India Synthetic
```

The directory is generated from `StudyRegistry.values`; it is not a hardcoded
prompt fragment. Full study overviews, table names, schema descriptions, and
publication excerpts are not injected on every turn.

`ToolContext` will own the authorized registry rather than one selected
bundle:

```python
@dataclass(frozen=True)
class ToolContext:
    studies: StudyRegistry
    artifact_store: ArtifactStore
    thread_id: str
    ...
```

Study-independent tools continue to ignore the registry. Study-dependent
tools resolve only logical study IDs through the registry; the model never
receives filesystem or Chroma paths.

The existing `active_study_id` must no longer bind tools to a bundle or act as
sticky thread state. A compatibility field may remain at an external API
boundary during migration, but it cannot silently determine a later tool
call. The exact study used by a tool must be observable in that call's
arguments, structured reference, or artifact provenance.

## Optional overview-based discovery

When the user names a valid study, the agent directly invokes the applicable
study search and does not call study discovery.

When labels and request text are insufficient, the agent may invoke
`search_studies()`. Version one takes no ranking query and returns a stable,
bounded page of installed-study overviews. Optional `offset` and `limit`
arguments default to the first page; at most five studies and 1,200 overview
characters per study are returned in one call:

```json
{
  "offset": 0,
  "returned_count": 1,
  "total_count": 1,
  "next_offset": null,
  "studies": [
    {
      "study_id": "nhanes-2017-2018",
      "label": "NHANES 2017-2018",
      "overview": "Bounded authoritative overview.md content",
      "overview_available": true
    }
  ]
}
```

Pagination exists only to preserve the model-message size contract as the
registry grows. With the initial two packages, one default call returns both
overviews. Entries use deterministic study-ID order, and the compact directory
already present in context lets the agent request another page when necessary.

The tool reads the existing free-form `study-design/overview.md` through the
package's study-design provider. It does not inspect table schemas, run a word
matching classifier, calculate a routing score, or bind a selected study. The
main LLM reads the returned overviews and retains responsibility for the
choice.

If one study clearly fits, the agent proceeds automatically. If two or more
remain genuinely plausible, the agent asks the user for clarification before
study-dependent catalog, publication, or design retrieval. This is model
judgment; the runtime will not maintain a deterministic resolved-concept or
study-selection ledger.

An installed study without an overview remains directly usable when the user
identifies it. Its discovery entry reports `overview_available: false` and a
bounded error description instead of failing the entire directory or
inventing routing metadata. RePORT India already supplies an overview. NHANES
must add a concise authoritative overview and the corresponding package
declaration in a new delivery version. No routing-profile file is introduced.

## DB-RAG discovery contract

`dbrag-search_catalog` remains a single-study operation and gains one required
scalar `study_id`:

```python
search_catalog(
    study_id="nhanes-2017-2018",
    queries=["glycated hemoglobin", "participant identifier"],
    limit=10,
)
```

The handler resolves the exact bundle with `context.studies.require()` and
searches only that bundle's semantic schema catalog. Existing behavior remains
unchanged within the selected catalog:

- at most five independent probes;
- at most ten results per probe;
- all per-probe results preserved;
- mandatory vector retrieval with deterministic lexical boosting;
- no lexical-only availability fallback;
- one bounded catalog-search observation artifact.

The model-facing response and saved observation include the selected
`study_id`. Hits expose complete structured identities:

```json
{
  "field_ref": {
    "study_id": "nhanes-2017-2018",
    "source_id": "nhanes-2017-2018",
    "table": "GHB_J",
    "column": "LBXGH"
  },
  "text": "Glycohemoglobin (%)",
  "matched_by": ["vector", "lexical"]
}
```

The shared reference models are:

```python
class TableRef(BaseModel):
    study_id: str
    source_id: str
    table: str

class FieldRef(BaseModel):
    study_id: str
    source_id: str
    table: str
    column: str
```

`study_id`, `source_id`, and `table` remain distinct. A study can contain
multiple sources, and a source can contain multiple tables.

## Exact inspection and relationship discovery

Downstream schema tools consume the structured references returned by catalog
search rather than accepting another loose study selector:

```python
inspect_table(table_ref=TableRef(...), offset=0, limit=25)

find_join_paths(
    required_fields=[FieldRef(...), FieldRef(...)],
    max_hops=3,
    max_paths=10,
)
```

`required_fields` identifies the exact fields that must be connected in one
dataset. `max_hops` bounds the number of join edges in a candidate path, and
`max_paths` bounds the number of alternatives returned. Relationship profiling
similarly consumes study-scoped table references and explicit key pairs.

Every handler validates the reference from the outside inward:

1. the study exists in the authorized registry;
2. the source belongs to that study;
3. the table belongs to that source;
4. the field belongs to that table;
5. every reference in one relationship operation has the same study ID.

No handler may replace a missing or invalid study with the previous, sole, or
default installed study.

## Dataset-plan boundary and lineage

`DatasetPlan` gains one required, immutable top-level `study_id`. Its existing
source, table, column, concept, filter, operation, and review structure remains
otherwise intact. This is the least disruptive plan migration: schema tools
use rich references at discovery time, while the frozen plan declares one
study boundary for all existing plan fields.

`dbrag-save_dataset_plan` validates that:

- the declared study is installed and authorized;
- every source in the plan belongs to that study;
- every table and column exists in that study's runtime catalog;
- every relationship stays within that study;
- a revision preserves the prior plan's study ID.

The saved artifact mirrors `study_id` in provenance. Existing plan artifacts
whose content lacks `study_id` may be normalized only when their immutable
artifact provenance contains one unambiguous study ID. A plan with neither is
stale and fails explicitly.

After plan creation, the agent does not resubmit a free-form study selector:

```text
validate_dataset_plan(plan_id, plan_version)
validate_and_extract(plan_id, plan_version)
inspect_dataset(dataset_id, dataset_version, plan_id, plan_version)
```

These tools resolve the bundle through plan or dataset lineage. Validated SQL,
extracted datasets, and dataset quality reports preserve the same study ID.
An artifact argument and resolved provenance that disagree produce an error;
the runtime never trusts the most recent conversational study.

## Other study-dependent retrieval

Publication and study-design discovery use the same per-call rule:

```python
publication-search_study_evidence(study_id=..., query=...)
study-design-search(study_id=..., query=...)
```

They resolve the requested bundle from the registry. Exact source-opening or
follow-up operations consume provenance-rich evidence references, or resolve
the study from the saved evidence artifact, so source IDs cannot collide
across packages. Existing mandatory semantic publication retrieval remains
mandatory and retains its typed failure when unavailable.

## Cross-study behavior

One conversation may search any number of studies in any order, including
switching back to an earlier study in a later turn. No selection is settled
for the entire thread.

One dataset plan, SQL statement, extraction, and dataset must belong to
exactly one study. Mixed-study relationship discovery and mixed-study plans
are rejected before SQL generation. SQL compilation authorizes only tables
from the plan's study, and execution opens only that study's DuckDB source.

A comparative request may create independent per-study plans and datasets:

```text
RePORT India       -> India plan       -> India dataset
RePORT Brazil      -> Brazil plan      -> Brazil dataset
RePORT Philippines -> Philippines plan -> Philippines dataset
```

The agent may compare separately derived summaries downstream, but the DB-RAG
layer does not row-link participants or issue cross-study SQL joins.

## Multiple calls and parallelism

`study_id` remains scalar. The agent may submit several independent read-only
catalog calls in one model response. Version one uses the current executor,
which accepts such a batch but invokes its calls sequentially.

True concurrent execution is a follow-up performance feature. The current
executor shares one mutable artifact store, failure list, activity stream,
state reducer, and cancellation flow across calls. Safely parallelizing it
requires isolated per-call state and deterministic result merging. That work
does not belong in the first multi-study correctness change.

Keeping scalar calls means later parallelization requires no model-facing tool
schema migration. It also keeps failures, results, output bounds, and
observation artifacts isolated per study.

## Failure behavior

The implementation uses explicit recoverable tool errors, including:

- `STUDY_NOT_AVAILABLE` for an unknown or unauthorized study ID;
- `CATALOG_UNAVAILABLE` when the selected study has no runtime catalog;
- `SEMANTIC_CATALOG_UNAVAILABLE` when mandatory semantic schema retrieval
  cannot run;
- `SOURCE_UNAVAILABLE` when a source is not present in the selected study;
- `STUDY_REFERENCE_MISMATCH` when nested reference provenance disagrees;
- `CROSS_STUDY_OPERATION_UNAVAILABLE` for mixed-study relationships or plans;
- `PLAN_STUDY_UNAVAILABLE` when a saved plan's study is no longer installed;
- `ARTIFACT_STUDY_PROVENANCE_MISSING` when legacy lineage cannot identify one
  study safely.

Errors may include a bounded installed-study directory to help the agent
repair an invalid ID. They must not silently retry another study, choose the
sole installed package, reuse a previous turn's study, or fall back to lexical
catalog retrieval.

## Compatibility and scope

This feature changes the study-selection boundary across DB-RAG, publication,
and study-design tools. It does not alter catalog ranking, table pagination,
join profiling algorithms, plan-review policy, SQL compiler semantics,
dataset-quality calculations, attachment tools, Python analysis, or the user
interface.

The application may retain legacy API/state fields long enough to load old
threads, but production study resolution must use explicit call arguments,
structured references, or artifact provenance. Compatibility code cannot
restore sticky active-study behavior.

Package work is limited to providing authoritative overview content where it
is absent, starting with a new NHANES delivery version. The feature does not
require a routing index, routing JSON, federated database, or rebuilding the
schema table and column collections solely for study selection.

## Verification

Focused tests will cover:

- registry-backed `ToolContext` with zero, one, two, and more studies;
- generated installed-study ID and label context;
- bounded overview discovery and missing-overview behavior;
- direct bypass of discovery for an explicit study;
- two studies containing the same source, table, or field names;
- correct per-study Chroma selection and absence of cross-study hits;
- correct table inspection through `TableRef`;
- correct relationship discovery through `FieldRef`;
- rejection of mismatched or mixed-study references;
- one-study plan validation and immutable study ID across revisions;
- legacy plan normalization from unambiguous provenance;
- plan, SQL, dataset, and quality-report study lineage;
- per-call publication and study-design scoping;
- study switching between turns and multiple searches in one turn;
- explicit rejection of cross-study SQL plans;
- no silent default-study, prior-study, or lexical-only fallback;
- unchanged study-independent tools and review behavior.

The dedicated internal feature smoke required by `AGENTS.md` will use the real
installed RePORT India and NHANES packages through production graph and tool
entry points. It will:

1. confirm both studies appear in the agent's compact directory;
2. retrieve their authoritative overviews;
3. run real semantic catalog searches against both isolated Chroma indexes;
4. inspect one returned table from each study;
5. prove that a mixed-study relationship or plan is rejected;
6. prove that two valid same-thread, different-study calls succeed without an
   active-study binding.

The smoke will use the configured real embedding dependency, run once with a
five-minute maximum, and preserve diagnostics on failure. Parallel timing is
not a success criterion for this version.
