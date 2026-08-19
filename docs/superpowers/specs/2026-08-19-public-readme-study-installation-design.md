# Public README and Fresh Study Installation Design

## Goal

Make the public README describe only the implemented local multi-study demo and
ensure its installation command works from the repository root after cloning.

## Repository contents

The repository root will contain and track exactly the current distributable
study archives used by the README:

- `report-india-synthetic-0.3.1.tar.gz`
- `nhanes-2017-2018-0.2.0.tar.gz`

The existing working-tree deletions of obsolete RePORT archives `0.2.0` and
`0.3.0` will be preserved. The private sibling `Database` directory remains
outside this repository and will not be referenced by public setup commands.

## README changes

The README will:

1. Remove the entire `Invitation-only hosted mode` section because a hosted
   deployment is not part of the delivered demo.
2. Remove the entire `Multi-study semantic catalog-binding smoke` section,
   including both internal smoke commands that depend on the private sibling
   `Database` directory.
3. Update `Start the demo` to install both root-level archives with:

   ```bash
   python study_installer.py --study \
     report-india-synthetic-0.3.1.tar.gz \
     nhanes-2017-2018-0.2.0.tar.gz
   ```

4. State that the two archives are included at the repository root and are
   installed together before starting FastAPI.

The local cancellation behavior, included-data description, Playwright note,
and safety note remain unchanged.

## Fresh-install boundary

The configured study root is
`/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/study_data`.
Implementation will delete that installer-managed directory in full, then run
the README command from `Epi-AI-Agent/`. This removes existing installed study
packages and their registry only.

The separate configured runtime root
`/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/runtime` is explicitly
out of scope and will not be deleted or modified by cleanup. It contains
conversations, uploads, generated datasets, and results.

If installation fails after cleanup, the source archives remain intact in the
repository root, so the same installer command can be rerun safely.

## Verification

Verification will confirm:

- both root archive files exist before cleanup;
- the exact bare-filename installer command exits successfully;
- `study_data/studies/registry.json` activates only
  `report-india-synthetic@0.3.1` and `nhanes-2017-2018@0.2.0`;
- no obsolete installed package versions remain under `study_data`;
- README no longer contains hosted-mode or internal multi-study smoke text;
- README contains the exact two-study installation command;
- relevant tracked tests and `git diff --check` pass.

