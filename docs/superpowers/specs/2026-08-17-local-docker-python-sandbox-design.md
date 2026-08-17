# Local Docker Python Sandbox Design

## Objective

Replace native execution of model-generated Python with a Docker-enforced
sandbox for the downloadable Epi-AI-Agent application. The application remains
a native Python/FastAPI process available only on the local machine. Each
analysis runs in a fresh, restricted Linux container. Users may add approved
Python packages to one persistent Docker volume without rebuilding the official
sandbox image.

The initial supported host platforms are macOS and Linux. Docker Desktop is the
supported runtime on macOS; Docker Engine or Docker Desktop is supported on
Linux. Windows, including native Windows and WSL2, is outside the initial
support boundary.

## Threat Model

The sandbox protects the user's host, application secrets, conversations,
uploads, study data, and other files from accidental or adversarial
model-generated Python. Generated code is untrusted even when the user's prompt
and selected dataset are trusted.

The sandbox does not claim to protect against a Docker daemon compromise,
malicious Docker Desktop or Docker Engine software, kernel or container-runtime
vulnerabilities, or a deliberately malicious package that exploits such a
vulnerability. Users remain responsible for deciding whether to install a
third-party package after Epi-AI-Agent presents its exact identity and proposed
dependency changes.

## Architecture

### Native application

FastAPI, LangGraph, storage, model calls, database retrieval, and the browser UI
continue to run natively. Local mode must bind to `127.0.0.1`; it must not
silently expose the unauthenticated local identity on `0.0.0.0` or another
non-loopback interface. The Compose port mapping, when used for development,
must bind its host side to `127.0.0.1` as well.

The application invokes the Docker CLI with argument arrays constructed by
trusted application code. Generated code, prompts, filenames, package names,
and model output may never supply Docker flags, mount sources, image names,
entrypoints, commands, or container names. Neither the analysis container nor
the package installer receives the Docker socket.

### Official sandbox image

The project publishes one versioned Linux sandbox image containing:

- Python 3.12;
- the existing analysis worker;
- the built-in scientific package set;
- a non-root runtime user; and
- metadata labels identifying the compatible worker protocol and sandbox
  release.

The image is multi-architecture for `linux/amd64` and `linux/arm64`. Each
application release selects it by immutable registry digest rather than by a
mutable tag. The setup workflow pulls the matching image and runs a self-test.
The image remains immutable after download.

### Per-analysis container

For each analysis, the application creates a private temporary execution
directory under the owning thread's execution storage. It serializes only the
generated code, dataset manifest, selected dataset identifier, and selected
dataset into an input directory. The result directory starts empty.

The application then starts a new container from the official image with fixed
controls:

- network disabled;
- non-root UID and GID fixed by application code;
- read-only root filesystem;
- all Linux capabilities dropped;
- `no-new-privileges` enabled;
- PID, CPU, memory, wall-time, and output-size limits;
- stdin closed;
- input directory mounted read-only;
- output directory mounted read-write;
- a size-limited temporary filesystem for runtime scratch space;
- no host environment inheritance beyond fixed locale/runtime values; and
- no API key, Docker socket, application source, runtime root, study root,
  conversation store, user home, or unrelated host path mounted or passed.

The container runs only the fixed worker entrypoint with fixed input and output
paths. Existing AST validation remains an early compatibility and mistake
filter, but Docker is the security boundary.

On success, the native application applies the existing bounded JSON and figure
validation before publishing a result. On timeout, cancellation, malformed
output, process failure, or application shutdown, it forcibly removes the
container and deletes incomplete temporary output.

There is no production fallback to native `exec`. If Docker is unavailable,
generated Python analysis is unavailable.

## Custom Package Volume

### Creation and layout

Users who only need built-in packages use the official image directly. No
custom-package volume is created.

After the first approved custom-package request, trusted application code
creates one Docker-managed volume for the current sandbox compatibility version.
The official image pre-creates the volume mount point with ownership assigned to
the fixed non-root sandbox user, allowing Docker's empty-volume initialization
to preserve writable ownership without a privileged installer. The volume
contains a Python virtual environment configured to see the official image's
system packages. Only user-approved packages and any additional or overriding
dependencies are physically stored in the volume; built-in packages are not
copied from the image.

The volume name is derived solely from fixed application and compatibility
version constants, not user or model input. A future Python ABI or sandbox
protocol change uses a new compatibility-version volume rather than silently
reusing an incompatible environment.

### Analysis use

When the custom environment exists and is healthy, each analysis container
mounts the same package volume read-only. The worker uses that environment so
Python resolves approved custom packages before compatible built-in packages.
Generated code cannot install, update, delete, or replace packages during an
analysis.

The package volume is not mounted when no custom environment exists. It is
never mounted read-write into an analysis container.

## Agent-Assisted Package Installation

### Dependency detection

If the worker cannot import a package, it returns a structured
`DEPENDENCY_NOT_AVAILABLE` failure containing the missing import name. The
application may map that import to a package only through a controlled package
resolution component. Model text alone is not authoritative for the package
source, version, registry, or installer arguments.

The initial package policy accepts normalized package names and exact versions
from the configured Python package index when compatible binary wheels are
available. It rejects source-only distributions, URLs, local paths, Git or other
VCS references, alternate registries supplied by model output, editable
installs, environment markers outside the supported sandbox platform, arbitrary
pip options, and shell syntax.

### Approval

Before approval, a temporary resolver container may contact only the configured
package index to retrieve and inspect package and wheel metadata. It receives no
Docker socket, secrets, datasets, application storage, or custom-package volume,
and it must not execute package build or runtime code. If the resolver cannot
produce a complete binary-wheel plan and hashes under those restrictions, the
request is unsupported.

Before downloading package payloads, executing third-party package code, or
mutating the custom-package volume, the run pauses at an explicit user approval.
The UI displays:

- the exact package and version requested;
- the missing import and why the analysis requested it;
- the configured package-index origin;
- the official sandbox image digest;
- the complete proposed additions, removals, upgrades, and downgrades returned
  by a resolver preview;
- a warning that third-party package installation and import execute third-party
  code; and
- the expected action after approval, including automatic analysis retry.

Approval is bound to the exact package plan. Any resolver change invalidates
the approval and requires a new one. Denial performs no installation. The agent
may then reformulate the analysis using available packages or explain why it
cannot complete the requested method.

### Installation container

After approval, the application starts a temporary installer container from the
same official sandbox image. It receives network access only to perform package
installation and mounts only the custom-package volume read-write. It does not
receive selected datasets, uploads, conversations, API keys, application
storage, host directories, or the Docker socket.

The installer runs as the sandbox's non-root user with dropped capabilities,
`no-new-privileges`, resource limits, a bounded temporary filesystem, and a
fixed installer command. Application code passes the already approved,
validated package plan without a shell. The installer updates the virtual
environment in the single package volume; it does not build or commit a new
Docker image.

After installation, a compatibility self-test imports the requested package,
starts the worker, verifies output creation, and confirms that analysis-time
network and filesystem restrictions remain effective. Only a passing
environment becomes active. The interrupted analysis then retries once from its
approved save point.

### Package state and recovery

The application stores outside the Docker volume:

- the requested top-level packages;
- the exact installed overlay packages and transitive versions;
- the resolver plan approved by the user;
- the official image digest and sandbox compatibility version;
- an append-only approval and installation event log;
- the active package-state hash; and
- installation and self-test status.

Every analysis result records the official image digest and active package-state
hash. This preserves provenance even as the active package volume changes.

If installation fails, the custom environment is marked unhealthy and is not
used for analysis. The application explains the failure without exposing
secrets and offers recovery from the last known-good locked package state.
Recovery recreates the custom-package volume from the official image and the
locked overlay packages. Removing or recreating the volume is an explicit,
user-approved operation because it is destructive. The official image and
built-in analysis environment remain available throughout recovery.

Package conflicts are shown before approval. The first release supports one
cumulative custom environment, not named parallel environments. If a requested
package cannot coexist with the active environment, installation is rejected
with the conflict details; named environments are deferred.

## Availability and User Experience

Startup checks the Docker CLI, daemon, selected image digest, architecture,
worker protocol, and sandbox self-test. Python analysis reports one of these
capability states through the existing runtime capability surface:

- ready with built-in packages;
- ready with a custom package-state hash;
- Docker CLI missing;
- Docker daemon unavailable;
- sandbox image missing or incompatible;
- custom package environment unhealthy; or
- sandbox self-test failed.

The rest of Epi-AI-Agent may start when Python analysis is unavailable. The UI
must clearly disable analysis actions and provide a specific remediation command
or explanation. It must never imply that analysis is sandboxed when the Docker
boundary has not passed its self-test.

Package installation, recovery, and cleanup are serialized so two requests
cannot mutate the package volume concurrently. Existing analyses keep their
recorded package-state identity. New analysis waits while an approved package
transaction is active.

## Error Handling

The Docker runtime translates infrastructure details into stable application
errors while preserving bounded diagnostic logs for local troubleshooting:

- Docker not installed or daemon stopped;
- image pull or digest mismatch;
- unsupported host architecture;
- container creation or startup failure;
- timeout, cancellation, memory exhaustion, PID exhaustion, or output overflow;
- missing or malformed worker result;
- missing dependency;
- denied package approval;
- package-resolution conflict;
- package installation or self-test failure; and
- cleanup failure.

Container identifiers are generated by trusted code, associated with one run,
and recorded so stale application-owned containers can be detected. Cleanup may
target only containers, volumes, and temporary paths carrying fixed
Epi-AI-Agent ownership labels and exact expected identifiers. The application
must never run broad Docker prune operations automatically.

## Testing and Verification

### Unit and contract tests

Tests cover:

- exact Docker argument construction and rejection of untrusted arguments;
- loopback-only local startup and Compose host-port binding;
- capability reporting for every Docker readiness state;
- input/output mount selection and path containment;
- image digest and compatibility-label validation;
- package-name and exact-version validation;
- resolver-plan approval binding;
- package transaction serialization;
- lock, audit, provenance, and recovery state;
- structured error translation; and
- cleanup targeting only labeled, run-owned resources.

### Real Docker integration tests

Real-container tests verify:

- successful statistical analysis and figure output;
- no network access from analysis;
- inability to read the host home, API key, application storage, or unrelated
  runtime files;
- read-only input, root filesystem, and custom-package volume;
- writable bounded output and scratch locations;
- non-root execution, dropped capabilities, and `no-new-privileges`;
- CPU, memory, PID, timeout, cancellation, and output limits;
- malformed output rejection and cleanup;
- operation with no custom-package volume;
- first approved package installation creating the volume;
- later approved packages accumulating in the same volume without rebuilding
  the official image;
- approval denial performing no mutation;
- dependency conflict reporting;
- failed installation preserving or recovering the last known-good state;
- successful import from the read-only package volume; and
- automatic one-time retry after a successful approved installation.

### Feature smoke

A dedicated real feature smoke launches the production FastAPI entrypoint and
compiled TypeScript frontend, uses the real sandbox image and Docker daemon,
drives the package-approval UI in a browser, and verifies raw API/LangGraph state
alongside rendered results. It follows the repository's five-minute, single-run
smoke requirement and preserves logs, screenshots, container inspection, and
artifacts on failure.

The release matrix covers supported macOS and Linux host workflows and both
`linux/amd64` and `linux/arm64` sandbox image builds. Windows behavior is not
reported as supported or passing.

## Distribution and Documentation

The terminal setup documentation lists Python 3.12 and Docker as prerequisites
for the complete application. It explains that the app runs locally, no
continuously hosted Epi-AI-Agent server is required, and users provide their own
OpenAI API key.

Release documentation includes the application checksum, official image digest,
supported host platforms and architectures, built-in package manifest, sandbox
self-test command, custom-package approval behavior, package-volume recovery,
and the trust implications of installing third-party packages.

## Non-Goals

The first release does not provide:

- native or WSL2 Windows support;
- public or multi-user hosting;
- native unsandboxed generated-code fallback;
- network access during analysis;
- in-analysis `pip install`;
- model-controlled Docker or pip arguments;
- arbitrary package sources, URLs, Git repositories, or registries;
- multiple named custom environments;
- automatic destructive Docker pruning; or
- protection from Docker, kernel, or container-runtime vulnerabilities.
