# AWS Runtime Directory Ownership Design

## Goal

Allow the unprivileged `epi-agent-web` service to create and use its SQLite
checkpoint database and user runtime directories on the retained EBS volume,
without broadening access or recursively changing stored user data.

## Observed Failure

Deployment command `26d3a84a-7f4b-48e9-8210-4e204543453e` successfully used
the corrected `api.app:app` ASGI entry point, accepted the fixed AWS Python
worker launcher, created a Python 3.12 environment, and installed all 148
dependencies. Application startup then failed with:

```text
sqlite3.OperationalError: unable to open database file
```

The requested path was:

```text
/srv/epi-agent/runtime/agent_memory_fastapi.db
```

The service runs as `epi-agent-web:epi-agent-web`, but EC2 bootstrap created
`/srv/epi-agent/runtime` as `root:epi-agent-web` with mode `0750`. The group can
read and traverse the directory but cannot create the SQLite database. The
systemd `ReadWritePaths` directive permits writes through the sandbox but does
not override Unix ownership and mode bits.

## Selected Design

Add the fixed runtime path to the root-owned immutable release installer and,
before release activation, run:

```bash
install -d -m 0750 -o epi-agent-web -g epi-agent-web /srv/epi-agent/runtime
```

GNU `install -d` applies the requested ownership and mode to the final
directory even when it already exists. The deployment mechanism invokes the
installer as root, so it can repair the directory created by the original
bootstrap.

The repair is deliberately non-recursive. It does not change the ownership or
mode of the checkpoint database, per-user folders, conversation artifacts,
execution directories, installed studies, releases, or any other retained EBS
content. `/srv/epi-agent` remains root-owned and non-writable by the service,
so the web user cannot replace the runtime directory entry itself.

Do not change CloudFormation, EC2 user data, systemd, EBS, the SQLite schema,
the checkpoint path, service identities, ACL behavior, study storage, or the
Python-worker launcher.

## Installer Ordering and Failure Behavior

The installer repairs the runtime directory after validating its fixed root
execution context and operator inputs, and before switching
`/opt/epi-agent/current` or starting `epi-agent.service`.

If ownership repair fails, `set -Eeuo pipefail` stops deployment before release
activation. The existing cleanup removes the maintenance marker and staging
directory. It does not delete the failed release, runtime directory, database,
or user data.

Every deployment reasserts the same fixed top-level ownership and mode. This
makes the correction self-healing for the current instance and future
instances without a separate one-off SSM patch or CloudFormation update.

## Regression Coverage

Before changing the installer:

1. Extend the real shell harness with a fake `install` executable that records
   arguments, removes only `-o` and `-g` for the local non-root fixture, and
   delegates the remaining operation to `/usr/bin/install`.
2. Add a test requiring the exact runtime repair arguments and requiring that
   invocation to occur before `systemctl enable epi-agent.service`.
3. Add the new regression to `scripts/smoke_aws_release_installer.py`.
4. Run the focused test and executable smoke against the current installer and
   record their expected failures.
5. Add the single fixed `install -d` production command and require the same
   checks to pass.
6. Run the complete installer, AWS, shell, startup-smoke, and CloudFormation
   validation gates before commit and independent review.

The harness exercises the real release-installer script with only privileged
host operations substituted, while the static host-asset test locks the exact
production path, owner, group, and mode.

## Deployment Boundary

EC2 instance `i-0f9ed9c133ea2358b` remains stopped during implementation and
review. After a clean reviewed commit:

1. Build and checksum a new immutable release; do not reuse any prior archive.
2. Upload and verify its exact S3 object once.
3. Start only the existing fixed EC2 instance and require SSM Online state.
4. Send at most one new deployment command.
5. Never retry failed command IDs
   `67a8ee05-4db7-431d-be98-443c54613253`,
   `84aa0bdd-aeaf-4c51-99f9-d1e13da343a1`,
   `b707be9e-b17f-44f4-9b36-955a246dc8f3`,
   `c3c8b05d-30de-448e-bdee-e7b5c8fd4ce3`, or
   `26d3a84a-7f4b-48e9-8210-4e204543453e`.
6. On deployment failure, collect evidence once and stop EC2 again.
7. On success, verify service, runtime ownership, local/public health and
   readiness, TLS, alarms, and unchanged EC2/EBS identities before uploading
   or installing study package `0.2.0`.

Prior S3 objects and failed inactive release directories remain available for
audit. The retained EBS volume and its data remain preserved.
