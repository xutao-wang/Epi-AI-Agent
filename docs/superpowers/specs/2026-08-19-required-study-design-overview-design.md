# Required Study-Design Overview Design

**Status:** Approved design, pending implementation plan

**Date:** 2026-08-19

## Goal

Every newly authored database study package must supply the authoritative
`study-design/overview.md` document. This applies uniformly to RePORT India,
MHANES, and any future study; no study-specific exceptions or defaults are
introduced.

Installed legacy packages remain readable. Their format-version-2 study-design
declaration remains optional and may use the existing legacy document format.

## Contract

For format-version-3 packages, `study_design` is required and is always the
Markdown declaration:

```json
"study_design": {
  "root": "study-design",
  "overview": "overview.md"
}
```

The manifest parser rejects a v3 package that omits `study_design`, uses a
different root, or uses a different overview name. Existing package validation
then verifies that the declared `study-design/overview.md` is a nonempty,
UTF-8 Markdown file within the existing 32 KiB limit and that its indexed
provenance matches the package content.

## Builder and Compatibility

The repository's package-fixture builder will emit the required v3 declaration
and write/index a default overview when creating a v3 test package. Explicit
fixture input can still provide alternate overview text or additional Markdown
documents. This keeps tests representative of all future builders while
avoiding hard-coded study content.

Version-2 packages keep their current behavior. The manifest model therefore
expresses a versioned compatibility boundary rather than treating missing study
design as a runtime capability choice for v3 packages.

## Error Handling

The parser should produce a clear validation error for a missing v3
`study_design` declaration. Existing installer errors remain responsible for a
missing, empty, invalid, oversized, or unsafe file after parsing.

## Tests

- A v3 manifest without `study_design` fails parsing.
- A v3 manifest with a root other than `study_design` fails parsing.
- A v3 fixture package is built with `study-design/overview.md` and passes
  staging/install validation.
- A v2 package without study design still parses, preserving legacy reads.
- Existing overview-content validation tests continue to protect the file-level
  contract.

## Scope

This change does not alter the study-routing design: the full overview remains
the authoritative routing evidence. It only guarantees that newly built v3
database study packages always provide that evidence.
