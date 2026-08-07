"""Contract tests for the Phase 2A AWS infrastructure templates."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_TEMPLATE = ROOT / "infra" / "aws" / "bootstrap" / "template.yaml"


class CloudFormationLoader(yaml.SafeLoader):
    """Parse CloudFormation short-form intrinsic functions as dictionaries."""


def _intrinsic(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node):
    value = loader.construct_scalar(node)
    return {"Ref" if tag_suffix == "Ref" else f"Fn::{tag_suffix}": value}


CloudFormationLoader.add_multi_constructor("!", _intrinsic)


def bootstrap_template() -> dict:
    return yaml.load(BOOTSTRAP_TEMPLATE.read_text(encoding="utf-8"), Loader=CloudFormationLoader)


def test_bootstrap_stack_uses_a_parameterized_developer_group() -> None:
    template = bootstrap_template()

    assert template["Parameters"]["DeveloperGroupName"] == {
        "Type": "String",
        "Default": "Developer",
    }


def test_bootstrap_stack_has_a_stable_cloudformation_execution_role() -> None:
    role = bootstrap_template()["Resources"]["CloudFormationExecutionRole"]

    assert role["Type"] == "AWS::IAM::Role"
    assert role["Properties"]["RoleName"] == "EpiAgentCloudFormationExecutionRole"
    principal = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Principal"]
    assert principal == {"Service": "cloudformation.amazonaws.com"}
    assert role["Properties"]["Tags"] == [
        {"Key": "Project", "Value": "epi-agent"},
        {"Key": "Environment", "Value": "phase2a"},
    ]


def test_developers_can_pass_only_the_generated_execution_role() -> None:
    policy = bootstrap_template()["Resources"]["DeveloperPassExecutionRolePolicy"]

    assert policy["Type"] == "AWS::IAM::Policy"
    assert policy["Properties"]["Groups"] == [{"Ref": "DeveloperGroupName"}]
    statement = policy["Properties"]["PolicyDocument"]["Statement"]
    assert statement == [
        {
            "Effect": "Allow",
            "Action": "iam:PassRole",
            "Resource": {"Fn::GetAtt": "CloudFormationExecutionRole.Arn"},
        }
    ]


def test_bootstrap_stack_does_not_create_human_iam_credentials_or_broad_passrole() -> None:
    template = bootstrap_template()
    resources = template["Resources"]
    rendered = BOOTSTRAP_TEMPLATE.read_text(encoding="utf-8")

    assert all(
        resource["Type"] not in {"AWS::IAM::User", "AWS::IAM::AccessKey"}
        for resource in resources.values()
    )
    assert "iam:PassRole\n        Resource: \"*\"" not in rendered
    assert "iam:CreateUser" not in rendered
    assert "iam:CreateAccessKey" not in rendered


def test_bootstrap_execution_policy_is_limited_to_phase_2a_services_and_resources() -> None:
    policy = bootstrap_template()["Resources"]["CloudFormationExecutionPolicy"]
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    rendered = BOOTSTRAP_TEMPLATE.read_text(encoding="utf-8")

    assert policy["Type"] == "AWS::IAM::Policy"
    assert policy["Properties"]["Roles"] == [{"Ref": "CloudFormationExecutionRole"}]
    assert {statement["Sid"] for statement in statements} == {
        "ManageNetworkAndCompute",
        "ManageEpiAgentStorage",
        "ManageCognito",
        "ManageDnsRecords",
        "ManageObservability",
        "ManageParametersAndLifecyclePolicies",
        "ManageEpiAgentRoles",
        "PassEpiAgentRolesToApprovedServices",
    }
    assert "iam:PassRole" in next(
        statement["Action"]
        for statement in statements
        if statement["Sid"] == "PassEpiAgentRolesToApprovedServices"
    )
    pass_role = next(
        statement for statement in statements if statement["Sid"] == "PassEpiAgentRolesToApprovedServices"
    )
    assert pass_role["Resource"] == {
        "Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/epi-agent-*"
    }
    assert pass_role["Condition"] == {
        "StringEquals": {"iam:PassedToService": ["ec2.amazonaws.com", "dlm.amazonaws.com"]}
    }
    for excluded in (
        "AWS::IAM::User",
        "AWS::IAM::AccessKey",
        "iam:CreateUser",
        "iam:CreateAccessKey",
        "iam:UpdateAccount",
        "iam:CreateAccountAlias",
        "organizations:",
        "route53:CreateHostedZone",
        "route53:DeleteHostedZone",
        "route53domains:",
    ):
        assert excluded not in rendered


def test_bootstrap_stack_exports_the_execution_role_arn() -> None:
    outputs = bootstrap_template()["Outputs"]

    assert outputs["CloudFormationExecutionRoleArn"] == {
        "Description": "ARN of the role CloudFormation assumes for Phase 2A stacks.",
        "Value": {"Fn::GetAtt": "CloudFormationExecutionRole.Arn"},
    }
