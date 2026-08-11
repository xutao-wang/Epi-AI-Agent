# AWS Study Installer Ownership Design

## Goal

Allow the `epi-agent-web` service to read the installed study registry and use
the writable Chroma index for `report-india-synthetic@0.2.0`, while keeping S3
download and outer-checksum verification in the root-owned host installer.

## Observed Failure

The authorized study upload completed successfully at:

```text
s3://epi-agent-phase2a-applicationbucket-who6tewa8rga/studies/report-india-synthetic-0.2.0.tar.gz
```

AWS reported AES256 encryption, SHA-256 metadata
`51a2603dff38de2020e2001fd967a970adca382fb39c94253a63afd84d94ff5e`, and
VersionId `RVOci4Qu34J_5F8zyLDmlheR6opyhBdg`.

Study-install command `6c1f1445-8e9b-470e-9fbc-72d2cf60a80b` downloaded the
archive, verified its checksum, validated it, and printed:

```text
Installed: report-india-synthetic@0.2.0
```

The following application restart failed because the root-run installer used
the Python package installer without dropping privileges. The atomic registry
writer consequently created:

```text
/srv/epi-agent/study_data/studies/registry.json  root:root  0600
```

The service runs as `epi-agent-web:epi-agent-web` and could not read that file.
Its restart loop reported:

```text
PermissionError: [Errno 13] Permission denied:
'/srv/epi-agent/study_data/studies/registry.json'
```

The retained installed package was also created as `root:root`. A local
read-only experiment confirmed that Chroma's `PersistentClient` attempts to
write its SQLite database even when the application is only opening an
existing index. Making only `registry.json` readable would therefore expose a
second failure: `attempt to write a readonly database`.

EC2 instance `i-0f9ed9c133ea2358b` is stopped. Both EBS volumes and the
installed study data are retained.

## Selected Design

Keep `/usr/local/sbin/install-study.sh` root-owned and require root for its
operator boundary. Root continues to:

1. validate all positional inputs;
2. create the fixed study root;
3. download the exact S3 object into a private staging directory; and
4. verify the supplied SHA-256 before any package code is invoked.

After checksum verification, the installer repairs the retained global study
tree for the fixed service identity using a physical, non-symlink-following
recursive ownership change:

```bash
chown -R -h epi-agent-web:epi-agent-web /srv/epi-agent/study_data
```

This operation is limited to the global study-package store. It does not touch
conversation history, checkpoints, user directories, generated artifacts,
release directories, or any other retained EBS content. It changes ownership
only; it does not delete package versions or rewrite their data.

Root also gives the verified staging directory to the service identity. The
Python study installer then runs with privileges dropped through the fixed
binary and a minimal environment:

```bash
/usr/sbin/runuser --user epi-agent-web -- /usr/bin/env -i \
  PATH=/opt/epi-agent/current/.venv/bin:/usr/bin \
  LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONUTF8=1 \
  REPORT_AGENT_STUDY_ROOT=/srv/epi-agent/study_data \
  /opt/epi-agent/current/.venv/bin/python \
  /opt/epi-agent/current/study_installer.py --study "$archive_path"
```

The same unprivileged identity performs the installed-manifest ID/version
verification. New registry and package files are therefore created with the
same identity that reads and uses them at application runtime.

The repair makes the already-installed `0.2.0` version idempotently reusable.
On the next invocation, the installer will validate the existing package,
confirm that its stored archive checksum matches, and retain the existing
version rather than delete or replace it.

## Security Boundary

- S3 access, archive download, and outer SHA-256 verification remain outside
  the unprivileged Python process.
- Package parsing and Chroma/DuckDB validation no longer run as root.
- `runuser`, `env`, Python, and the installer paths are fixed absolute paths.
- The environment does not carry AWS credentials, provider API keys, or a
  caller-controlled `PATH` into the Python installer.
- The recursive repair uses `chown -R -h` and therefore changes symlink
  ownership rather than dereferencing symlink targets.
- The application service receives write access only to its existing
  `ReadWritePaths`, including `/srv/epi-agent/study_data`; systemd restrictions
  and the root-owned release tree remain unchanged.

## Ordering and Failure Behavior

The installer order is fixed:

1. require root and validate operator inputs;
2. prepare the study root and private staging directory;
3. download and checksum the immutable archive;
4. repair retained study and staging ownership;
5. drop privileges and install or reuse the requested package;
6. verify the installed manifest as `epi-agent-web`; and
7. remove only the temporary staging directory through the existing cleanup
   trap.

If download or checksum verification fails, no recursive retained-study or
registry ownership changes occur; only the fixed top-level study directory may
have its expected owner and mode reasserted. If package validation or
activation fails after privilege drop, the Python installer's existing staging
and promotion logic preserves the
previous registry and removes incomplete promoted data. The shell cleanup does
not delete installed packages, the registry, or other retained study versions.

## Regression Coverage

Implementation follows strict RED-GREEN TDD and extends the real shell harness
for the study installer. Tests must prove:

1. root validation, S3 download, and SHA-256 verification precede privilege
   dropping;
2. the retained study tree and verified staging directory are handed to
   `epi-agent-web` with the exact non-dereferencing ownership command;
3. the Python installer and installed-manifest check both execute through
   `/usr/sbin/runuser --user epi-agent-web -- /usr/bin/env -i`;
4. the minimal environment contains only the fixed runtime variables required
   by the installer;
5. a retained root-owned registry and Chroma tree become readable and writable
   by the simulated service identity without changing sentinel contents;
6. checksum failure occurs before ownership repair or privilege dropping;
7. install failure leaves prior registry/package data intact and clears only
   temporary staging; and
8. the existing release-installer, AWS infrastructure, study-package, startup,
   shell-syntax, and smoke regressions remain green.

## Deployment and Recovery Boundary

The original deploy-first sequence for this correction is superseded by
`2026-08-11-aws-study-access-recovery-design.md`. The current application
cannot pass the release installer's drain/health gates until retained study
ownership is repaired, so the dedicated recovery document must succeed before
another release deployment is attempted.

Do not re-upload the study object and do not retry a failed SSM command ID. On
any new live failure, collect evidence once and stop EC2 again. The current
study data and prior application releases remain available for recovery and
audit.
