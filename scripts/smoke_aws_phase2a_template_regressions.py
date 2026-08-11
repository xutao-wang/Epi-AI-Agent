#!/usr/bin/env python3.12
"""Exercise the deployment-template contracts missed by CloudFormation validation."""

from __future__ import annotations

import re
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

    phase2a = _load("infra/aws/phase2a/template.yaml")
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
