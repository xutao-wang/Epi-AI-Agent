# AWS Python-Worker Launcher Compatibility Design

## Goal

Allow the AWS application to start with its existing, security-constrained
Python-worker configuration without changing live infrastructure or weakening
the worker privilege boundary.

## Observed Failure

Release `153c64bb7894d32bcb5646fe37c8c99c55cf903b` corrected the ASGI target and
successfully imported `api.app`. Deployment command
`c3c8b05d-30de-448e-bdee-e7b5c8fd4ce3` then failed once during application
startup with:

```text
ValueError: REPORT_AGENT_PYTHON_WORKER_LAUNCHER must be the fixed worker path
```

The AWS-generated `/etc/epi-agent/app.env` supplies:

```text
/usr/bin/sudo -n /usr/local/libexec/epi-agent-python-worker
```

`api.deployment.python_worker_launcher()` currently accepts only:

```text
/usr/local/libexec/epi-agent-python-worker
```

and then returns the same fixed sudo-prefixed command that AWS already
supplies. The infrastructure and application therefore describe the same
intended command using two different configuration forms.

## Selected Design

Teach `python_worker_launcher()` to accept exactly two configurations:

1. `/usr/local/libexec/epi-agent-python-worker`
2. `/usr/bin/sudo -n /usr/local/libexec/epi-agent-python-worker`

Both inputs normalize to the immutable tuple:

```python
(
    "/usr/bin/sudo",
    "-n",
    "/usr/local/libexec/epi-agent-python-worker",
)
```

Continue rejecting relative paths, alternate executables, additional flags,
shell separators, embedded line breaks, null bytes, and every other token
sequence. The environment does not gain authority to choose an executable or
sudo option.

Do not edit CloudFormation, EC2 user data, `/etc/epi-agent/app.env`, the worker
wrapper, sudoers configuration, or the Python runtime. This compatibility
normalization fixes the existing instance and preserves the current host
security model.

## Data Flow

At application startup, `api.app.build_application()` reads
`REPORT_AGENT_PYTHON_WORKER_LAUNCHER` and passes it to
`python_worker_launcher()`. The resolver tokenizes the value, matches it
against the two exact accepted tuples, and returns the single canonical sudo
tuple. `LocalPythonRuntime` later invokes only that canonical tuple plus its
internally controlled worker arguments.

Invalid configuration fails closed during startup with the existing
`ValueError`. No fallback to direct or shell-based execution is added.

## Regression Coverage

Before changing production code:

1. Change the hosted-launcher unit test to require the AWS sudo-prefixed value
   to normalize to the canonical tuple.
2. Keep explicit rejection coverage for alternate paths and appended options.
3. Change `scripts/smoke_aws_service_entrypoint.py` to use the actual AWS
   sudo-prefixed environment value instead of disabling the worker launcher.
4. Run the focused unit test and executable smoke against current code and
   record both expected failures.
5. Implement the smallest resolver change and require the same checks to pass.
6. Run the affected startup, runtime, AWS, smoke, shell, and CloudFormation
   validation gates before commit and review.

The smoke continues to derive `api.app:app` from the real systemd unit, so it
now covers both production startup boundaries that caused the two failed
releases.

## Deployment Boundary

EC2 instance `i-0f9ed9c133ea2358b` remains stopped during implementation and
review. After a clean reviewed commit:

1. Build and checksum a new immutable release; do not reuse either failed
   archive.
2. Upload its exact S3 object once.
3. Start the existing EC2 instance and require SSM Online state.
4. Send at most one new release deployment command.
5. Never retry failed command IDs
   `67a8ee05-4db7-431d-be98-443c54613253`,
   `84aa0bdd-aeaf-4c51-99f9-d1e13da343a1`,
   `b707be9e-b17f-44f4-9b36-955a246dc8f3`, or
   `c3c8b05d-30de-448e-bdee-e7b5c8fd4ce3`.
6. Verify service, local/public health and readiness, TLS, alarms, and unchanged
   EC2/EBS identities before uploading or installing study package `0.2.0`.

Failed release directories and prior S3 versions remain available for audit.
EBS data is preserved while the instance is stopped and through the next
deployment.
