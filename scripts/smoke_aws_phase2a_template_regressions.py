#!/usr/bin/env python3.12
"""Exercise the deployment-template contracts missed by CloudFormation validation."""

from __future__ import annotations

import io
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class CloudFormationLoader(yaml.SafeLoader):
    """Parse scalar CloudFormation intrinsic functions for smoke inspection."""


def _intrinsic(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> dict[str, str]:
    value = loader.construct_scalar(node)
    return {"Ref" if tag_suffix == "Ref" else f"Fn::{tag_suffix}": value}


CloudFormationLoader.add_multi_constructor("!", _intrinsic)


def _load(relative_path: str) -> dict:
    path = ROOT / relative_path
    return yaml.load(path.read_text(encoding="utf-8"), Loader=CloudFormationLoader)


def assert_release_archive_extractor_works(template: dict) -> None:
    """Run the release archive extractor embedded in the Phase 2A SSM document."""
    run_command = template["Resources"]["EpiAgentDeployReleaseDocument"]["Properties"][
        "Content"
    ]["mainSteps"][0]["inputs"]["runCommand"]
    command = "\n".join(run_command)
    marker = '/usr/bin/python3.12 - "$staging_dir/release.tar.gz" "$staging_dir/release" <<\'PY\''
    source_start = command.find(marker)
    if source_start == -1:
        raise AssertionError("release archive extractor heredoc is missing")
    source_start += len(marker)
    source_end = command.find("\nPY\n", source_start)
    if source_end == -1:
        raise AssertionError("release archive extractor heredoc terminator is missing")
    source = command[source_start:source_end]

    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        archive_path = temporary_path / "release.tar.gz"
        destination = temporary_path / "release"
        payload = b"extractor-ok\n"
        with tarfile.open(archive_path, "w:gz") as archive:
            member = tarfile.TarInfo("payload.txt")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        completed = subprocess.run(
            [sys.executable, "-", str(archive_path), str(destination)],
            input=source,
            text=True,
            capture_output=True,
            check=False,
        )
        payload_path = destination / "payload.txt"
        if completed.returncode != 0 or not payload_path.is_file() or payload_path.read_bytes() != payload:
            raise AssertionError(completed.stderr)


def main() -> None:
    bootstrap = _load("infra/aws/bootstrap/template.yaml")
    statements = bootstrap["Resources"]["CloudFormationExecutionPolicy"]["Properties"][
        "PolicyDocument"
    ]["Statement"]
    cognito = next(statement for statement in statements if statement["Sid"] == "ManageCognito")
    required_tag_actions = {"cognito-idp:TagResource", "cognito-idp:UntagResource"}
    if not required_tag_actions <= set(cognito["Action"]):
        raise AssertionError("CloudFormation cannot manage the tagged Cognito user pool")
    route53 = next(statement for statement in statements if statement["Sid"] == "ManageDnsRecords")
    if "route53:GetHostedZone" not in route53["Action"]:
        raise AssertionError("CloudFormation cannot verify the hosted zone before changing DNS")
    documents = next(
        statement for statement in statements if statement["Sid"] == "ManageEpiAgentSsmDocuments"
    )
    required_document_actions = {
        "ssm:GetDocument",
        "ssm:UpdateDocumentDefaultVersion",
    }
    if not required_document_actions <= set(documents["Action"]):
        raise AssertionError("CloudFormation cannot read and select the current SSM document version")
    if documents["Resource"] != {
        "Fn::Sub": "arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:document/epi-agent-*"
    }:
        raise AssertionError("SSM document lifecycle permissions must remain scoped to epi-agent documents")

    phase2a = _load("infra/aws/phase2a/template.yaml")
    recovery = phase2a["Resources"]["EpiAgentRecoverStudyAccessDocument"]["Properties"]
    if recovery["Name"] != "epi-agent-recover-study-access":
        raise AssertionError("study access recovery document name changed")
    if recovery.get("UpdateMethod") != "NewVersion":
        raise AssertionError("study access recovery updates must create a new version")
    recovery_content = recovery["Content"]
    if "parameters" in recovery_content:
        raise AssertionError("study access recovery must remain parameter-free")
    recovery_command = "\n".join(
        recovery_content["mainSteps"][0]["inputs"]["runCommand"]
    )
    recovery_order = (
        "chown -R -h epi-agent-web:epi-agent-web",
        "/usr/sbin/runuser --user epi-agent-web -- /usr/bin/env -i",
        "systemctl restart epi-agent.service",
        "http://127.0.0.1:8000/api/health",
        "http://127.0.0.1:8000/api/readiness",
    )
    positions = [recovery_command.index(token) for token in recovery_order]
    if positions != sorted(positions):
        raise AssertionError("study access recovery command order is unsafe")
    if any(token in recovery_command for token in ("eval ", "bash -c", "rm -rf")):
        raise AssertionError("study access recovery contains an unsafe shell operation")
    if phase2a["Outputs"]["RecoverStudyAccessDocumentName"]["Value"] != {
        "Ref": "EpiAgentRecoverStudyAccessDocument"
    }:
        raise AssertionError("study access recovery output does not match its document")
    assert_release_archive_extractor_works(phase2a)
    lifecycle = phase2a["Resources"]["ApplicationDataVolumeLifecyclePolicy"]["Properties"]
    if not re.fullmatch(r"[0-9A-Za-z _-]+", lifecycle["Description"]):
        raise AssertionError("the DLM description contains characters rejected by AWS")
    parameters = phase2a["Resources"]["EpiAgentDeployReleaseDocument"]["Properties"]["Content"][
        "parameters"
    ]
    parameter_names = (
        "Bucket",
        "ReleaseKey",
        "ReleaseSha256",
        "ReleaseId",
        "DomainName",
        "CertificateEmail",
    )
    inputs = phase2a["Resources"]["EpiAgentDeployReleaseDocument"]["Properties"][
        "Content"
    ]["mainSteps"][0]["inputs"]
    if "interpolationType" in inputs:
        raise AssertionError("ENV_VAR interpolation must be declared on SSM parameters")
    for name in parameter_names:
        if parameters[name].get("interpolationType") != "ENV_VAR":
            raise AssertionError(f"{name} does not export its SSM environment variable")
    patterns = {name: parameter["allowedPattern"] for name, parameter in parameters.items()}
    unsupported = ("(?=", "(?!", "(?<=", "(?<!")
    for name, pattern in patterns.items():
        if any(token in pattern for token in unsupported):
            raise AssertionError(f"{name} uses lookaround unsupported by AWS SSM: {pattern}")

    release_key = re.compile(patterns["ReleaseKey"])
    if not release_key.fullmatch("releases/0123456789abcdef.tar.gz"):
        raise AssertionError("the release-key pattern rejects a valid immutable package")
    for unsafe_key in (
        "releases/../secrets",
        "releases/package..tar.gz",
        "releases//package.tar.gz",
    ):
        if release_key.fullmatch(unsafe_key):
            raise AssertionError(f"the release-key pattern accepts unsafe key {unsafe_key!r}")

    domain_name = re.compile(patterns["DomainName"])
    if not domain_name.fullmatch("epiagent.org"):
        raise AssertionError("the SSM domain pattern rejects the production domain")
    for invalid_domain in ("-epiagent.org", "epiagent-.org"):
        if domain_name.fullmatch(invalid_domain):
            raise AssertionError(f"the SSM domain pattern accepts invalid domain {invalid_domain!r}")

    print("Phase 2A deployment-template regression smoke passed")


if __name__ == "__main__":
    main()
