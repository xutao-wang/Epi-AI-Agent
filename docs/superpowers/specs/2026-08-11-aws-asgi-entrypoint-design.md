# AWS ASGI Entry-Point Correction Design

## Goal

Start the configured Epi Agent FastAPI application on AWS by pointing the
systemd service at the module that actually exports the application object.

## Observed Failure

SSM command `b707be9e-b17f-44f4-9b36-955a246dc8f3` successfully downloaded and
checksum-verified application release
`004186c18f9ed9ea4e2a0939cf63eecee4d4a411`, extracted it, created its Python
3.12 environment, and installed all 148 dependencies. The bounded startup gate
then correctly detected that the process exited instead of becoming healthy.

The application log reported:

```text
ERROR: Error loading ASGI app. Attribute "app" not found in module "api.server".
```

`deploy/aws/systemd/epi-agent.service` launches `uvicorn api.server:app`, but
`api.server` exports the `create_app(...)` route factory, not a configured
application object. `api.app` builds the AWS/local configuration and exports
the actual `app` object. Existing browser smoke infrastructure already launches
`api.app:app`.

## Selected Design

Change only the systemd Uvicorn target from `api.server:app` to `api.app:app`.
Keep the host, port, worker count, service user, sandboxing, filesystem access,
restart policy, environment file, and logging configuration unchanged.

Do not add an alias to `api.server`; that would couple route construction to
startup configuration and introduce circular-import risk. Do not switch to
Uvicorn factory mode because the repository already has one canonical configured
application object.

## Regression Coverage

Before editing the service unit:

1. Strengthen the host-asset test to require the exact `api.app:app` target and
   reject `api.server:app`.
2. Add an executable smoke under `scripts/` that reads the real systemd
   `ExecStart`, extracts the ASGI module/object target, prepares isolated local
   runtime/study paths and local authentication configuration, imports that
   exact target, and requires it to be a FastAPI application.
3. Verify both checks fail against the current unit for the observed reason.
4. Apply the one-target production correction and require both checks to pass.
5. Run the complete AWS host/infrastructure/CLI suites, shell syntax checks,
   existing AWS smokes, release build validation, and CloudFormation validation.

The executable smoke tests the entry point named by the service file rather
than importing a separately hard-coded known-good module.

## Deployment and Failure Boundaries

After test-first implementation and independent review:

1. Build a new clean commit-identified application archive; do not reuse
   release `004186c`.
2. Upload and verify its exact S3 object once.
3. Invoke SSM once with the new release. Never retry commands
   `67a8ee05-4db7-431d-be98-443c54613253`,
   `84aa0bdd-aeaf-4c51-99f9-d1e13da343a1`, or
   `b707be9e-b17f-44f4-9b36-955a246dc8f3`.
4. On success, verify local and public health/readiness, TLS, service enablement,
   alarms, and unchanged EC2/EBS identities.
5. Only after application success, upload and install the separately verified
   synthetic `report-india-synthetic-0.2.0.tar.gz` package once.

No CloudFormation, EC2, EBS, VPC, IAM, Cognito, Route 53, database format, or
user-data change is required. Failed inactive release directories and prior S3
objects remain untouched.
