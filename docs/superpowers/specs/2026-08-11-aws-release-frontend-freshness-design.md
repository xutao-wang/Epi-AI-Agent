# AWS Release Frontend Freshness Design

## Purpose

Prevent an immutable AWS application release from combining current tracked
frontend source with an older tracked `frontend/dist` browser bundle.

Release preparation discovered that four tracked frontend inputs no longer
match `frontend/dist/build-manifest.json`. The existing release builder checks
that this manifest exists and records its checksum, but it does not verify the
manifest against current frontend inputs. A release built in that state would
be internally inconsistent even though its archive checksum was correct.

## Chosen Approach

Add a fail-closed frontend freshness check to `scripts/build_aws_release.py`,
then regenerate and commit the tracked production frontend bundle and build
manifest before building the AWS archive.

This is preferred over rebuilding only once because the builder will prevent
the same stale-asset mistake in future releases.

## Frontend Input Contract

The release builder derives the expected frontend input set from tracked files:

- every regular tracked file beneath `frontend/src/`; and
- the tracked build inputs `frontend/index.html`, `frontend/package-lock.json`,
  `frontend/package.json`, `frontend/tsconfig.json`,
  `frontend/tsconfig.node.json`, and `frontend/vite.config.ts`.

The tracked `frontend/dist/build-manifest.json` must contain a
`source_sha256` object with exactly that path set. Every recorded digest must
equal the current file's SHA-256 digest. The manifest's `vite_version` must
equal the version pinned for `node_modules/vite` in
`frontend/package-lock.json`.

The builder fails before writing a release archive when the manifest is
missing, invalid, has a missing or extra source path, has a mismatched source
digest, or records the wrong Vite version.

The manifest's informational `built_at` value is not part of the deterministic
archive decision beyond being included in the tracked manifest bytes.

## Regeneration Workflow

1. Run the frontend unit suite.
2. Run `npm --prefix frontend run build` to regenerate `frontend/dist`.
3. Refresh `frontend/dist/build-manifest.json` from the exact tracked frontend
   input set and installed Vite version.
4. Verify the refreshed manifest against current tracked inputs.
5. Review generated asset additions/deletions and commit the builder,
   regression tests, production bundle, and manifest together.
6. Treat that new commit SHA—not `6df62bc`—as the immutable AWS release ID.
7. From the clean new commit, run release-focused tests and build the three
   ignored artifacts under `dist/aws/`: `.tar.gz`, `.sha256`, and `.json`.

The existing ignored working-demo helper may be used to generate the manifest,
but its unrelated repository ignore-policy findings do not authorize changes
outside this release-safety scope. The committed release-builder check is the
authoritative archive gate.

## Tests

Use RED-GREEN TDD in `tests/test_build_aws_release.py`:

- a fixture with a valid current build manifest can build;
- changing a tracked frontend input after the manifest is written causes
  `ReleaseBuildError` before archive creation;
- adding a new tracked frontend source absent from the manifest is rejected;
- a wrong Vite version is rejected; and
- existing determinism, exclusion, clean-tree, safe-path, and commit-identity
  tests remain green.

After regenerating the actual frontend, run:

- the complete frontend unit suite;
- the production frontend build;
- the release-builder tests;
- AWS host-asset and installer regression/smoke tests relevant to the archive;
- archive checksum validation;
- an archive content audit requiring the corrected systemd unit,
  `api/deployment.py`, current `frontend/dist`, and embedded `release.json`;
  and rejecting `.env`, `.env.*`, `runtime/`, `study_data/`, raw study
  archives, unsafe paths, and non-regular members.

Build twice from the same clean commit and require byte-identical archives and
sidecars.

## Safety and Authorization Boundary

This correction and release build are local-only. They do not authorize:

- uploading the archive to S3;
- starting EC2;
- invoking `epi-agent-recover-study-access`;
- invoking `epi-agent-deploy-release`;
- installing a study package; or
- changing any other AWS resource.

After local verification, report the new full commit SHA, archive path,
archive SHA-256, manifest path, and proposed S3 key. Stop for separate explicit
upload authorization.

No provider key, runtime database, conversation, artifact, installed study,
raw study archive, or other retained data is included in the release.
