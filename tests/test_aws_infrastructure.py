"""Contract tests for the Phase 2A AWS infrastructure templates."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_TEMPLATE = ROOT / "infra" / "aws" / "bootstrap" / "template.yaml"
PHASE2A_TEMPLATE = ROOT / "infra" / "aws" / "phase2a" / "template.yaml"
PHASE2A_PARAMETERS = ROOT / "infra" / "aws" / "phase2a" / "parameters.example.json"
PHASE2A_LINT_REQUIREMENTS = ROOT / "infra" / "aws" / "phase2a" / "cfn-lint-requirements.txt"


class CloudFormationLoader(yaml.SafeLoader):
    """Parse CloudFormation short-form intrinsic functions as dictionaries."""


def _intrinsic(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node):
    value = loader.construct_scalar(node)
    return {"Ref" if tag_suffix == "Ref" else f"Fn::{tag_suffix}": value}


CloudFormationLoader.add_multi_constructor("!", _intrinsic)


def bootstrap_template() -> dict:
    return yaml.load(BOOTSTRAP_TEMPLATE.read_text(encoding="utf-8"), Loader=CloudFormationLoader)


def phase2a_template() -> dict:
    return yaml.load(PHASE2A_TEMPLATE.read_text(encoding="utf-8"), Loader=CloudFormationLoader)


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
        "ManageEpiAgentRoleReadAndDeletion",
        "CreateEpiAgentRoleWithBoundary",
        "MutateEpiAgentRolePermissionsAndTrustWithBoundary",
        "PassEpiAgentRolesToApprovedServices",
        "ReadDnsChanges",
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
    mutations = {
        "iam:CreateRole",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:UpdateAssumeRolePolicy",
    }
    boundary_arn = {"Fn::GetAtt": "EpiAgentWorkloadBoundary.PolicyArn"}
    mutation_statements = [
        statement
        for statement in statements
        if mutations.intersection(
            {statement["Action"]}
            if isinstance(statement["Action"], str)
            else set(statement["Action"])
        )
    ]
    assert mutation_statements
    seen_mutations: set[str] = set()
    for statement in mutation_statements:
        actions = {statement["Action"]} if isinstance(statement["Action"], str) else set(statement["Action"])
        assert actions <= mutations
        seen_mutations.update(actions)
        assert statement["Condition"] == {
            "StringEquals": {"iam:PermissionsBoundary": boundary_arn}
        }
    assert seen_mutations == mutations
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
        "iam:CreatePolicy",
        "iam:DeletePolicy",
        "iam:CreatePolicyVersion",
        "iam:DeletePolicyVersion",
        "iam:SetDefaultPolicyVersion",
        "iam:PutRolePermissionsBoundary",
        "iam:DeleteRolePermissionsBoundary",
    ):
        assert excluded not in rendered


def test_bootstrap_workload_boundary_limits_workload_roles_without_iam_or_sts() -> None:
    template = bootstrap_template()
    resources = template["Resources"]
    boundary = resources["EpiAgentWorkloadBoundary"]

    assert boundary["Type"] == "AWS::IAM::ManagedPolicy"
    assert boundary["Properties"]["ManagedPolicyName"] == "epi-agent-workload-boundary"
    assert boundary["Metadata"] == {"Project": "epi-agent", "Environment": "phase2a"}
    assert "PermissionsBoundary" not in resources["CloudFormationExecutionRole"]["Properties"]
    actions = {
        action
        for statement in boundary["Properties"]["PolicyDocument"]["Statement"]
        for action in (
            [statement["Action"]]
            if isinstance(statement["Action"], str)
            else statement["Action"]
        )
    }
    assert actions
    assert all(action.split(":", 1)[0] in {"ec2", "logs", "s3", "ssm"} for action in actions)
    assert all(not action.startswith(("iam:", "sts:")) for action in actions)


def test_bootstrap_stack_scopes_route53_change_lookup_to_change_arns() -> None:
    statements = bootstrap_template()["Resources"]["CloudFormationExecutionPolicy"]["Properties"][
        "PolicyDocument"
    ]["Statement"]

    records = next(statement for statement in statements if statement["Sid"] == "ManageDnsRecords")
    changes = next(statement for statement in statements if statement["Sid"] == "ReadDnsChanges")
    assert records["Action"] == [
        "route53:ChangeResourceRecordSets",
        "route53:ListResourceRecordSets",
    ]
    assert records["Resource"] == {"Fn::Sub": "arn:${AWS::Partition}:route53:::hostedzone/*"}
    assert changes == {
        "Sid": "ReadDnsChanges",
        "Effect": "Allow",
        "Action": "route53:GetChange",
        "Resource": {"Fn::Sub": "arn:${AWS::Partition}:route53:::change/*"},
    }


def test_bootstrap_stack_exports_the_execution_role_arn() -> None:
    outputs = bootstrap_template()["Outputs"]

    assert outputs["CloudFormationExecutionRoleArn"] == {
        "Description": "ARN of the role CloudFormation assumes for Phase 2A stacks.",
        "Value": {"Fn::GetAtt": "CloudFormationExecutionRole.Arn"},
    }


def test_phase2a_network_has_only_the_required_low_cost_resources() -> None:
    template = phase2a_template()
    resources = template["Resources"]
    types = Counter(resource["Type"] for resource in resources.values())

    assert types["AWS::EC2::VPC"] == 1
    assert types["AWS::EC2::Subnet"] == 1
    assert types["AWS::EC2::InternetGateway"] == 1
    assert types["AWS::EC2::RouteTable"] == 1
    assert types["AWS::EC2::VPCEndpoint"] == 1
    assert types["AWS::EC2::SecurityGroup"] == 1
    forbidden_resource_prefixes = (
        "AWS::CloudFront::",
        "AWS::ElasticLoadBalancing",
        "AWS::RDS::",
        "AWS::ElastiCache::",
        "AWS::ECS::",
        "AWS::EKS::",
        "AWS::EC2::NatGateway",
    )
    assert all(
        not resource_type.startswith(forbidden_prefix)
        for resource_type in types
        for forbidden_prefix in forbidden_resource_prefixes
    )

    vpc = resources["ApplicationVpc"]["Properties"]
    assert vpc["CidrBlock"] == "10.42.0.0/24"
    assert vpc["EnableDnsSupport"] is True
    assert vpc["EnableDnsHostnames"] is True
    assert resources["PublicSubnet"]["Properties"]["CidrBlock"] == "10.42.0.0/26"
    assert resources["DefaultRoute"]["Properties"] == {
        "RouteTableId": {"Ref": "PublicRouteTable"},
        "DestinationCidrBlock": "0.0.0.0/0",
        "GatewayId": {"Ref": "InternetGateway"},
    }
    assert resources["S3GatewayEndpoint"]["Properties"]["VpcEndpointType"] == "Gateway"
    assert resources["S3GatewayEndpoint"]["Properties"]["ServiceName"] == {
        "Fn::Sub": "com.amazonaws.${AWS::Region}.s3"
    }


def test_phase2a_security_group_exposes_only_https_and_http_without_ssh() -> None:
    group = phase2a_template()["Resources"]["ApplicationSecurityGroup"]["Properties"]

    assert group["SecurityGroupIngress"] == [
        {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "CidrIp": "0.0.0.0/0"},
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "CidrIp": "0.0.0.0/0"},
    ]
    assert group["SecurityGroupEgress"] == [
        {
            "Description": "HTTPS for AWS services and package repositories.",
            "IpProtocol": "tcp",
            "FromPort": 443,
            "ToPort": 443,
            "CidrIp": "0.0.0.0/0",
        },
        {
            "Description": "DNS over TCP.",
            "IpProtocol": "tcp",
            "FromPort": 53,
            "ToPort": 53,
            "CidrIp": "0.0.0.0/0",
        },
        {
            "Description": "DNS over UDP.",
            "IpProtocol": "udp",
            "FromPort": 53,
            "ToPort": 53,
            "CidrIp": "0.0.0.0/0",
        },
    ]


def test_phase2a_retains_encrypted_data_volume_and_private_s3_bucket() -> None:
    template = phase2a_template()
    resources = template["Resources"]
    types = Counter(resource["Type"] for resource in resources.values())

    assert types["AWS::EC2::Volume"] == 1
    assert types["AWS::S3::Bucket"] == 1
    assert types["AWS::S3::BucketPolicy"] == 1
    volume = resources["ApplicationDataVolume"]
    assert volume["DeletionPolicy"] == "Retain"
    assert volume["UpdateReplacePolicy"] == "Retain"
    assert volume["Properties"] == {
        "AvailabilityZone": {"Fn::GetAtt": "PublicSubnet.AvailabilityZone"},
        "Encrypted": True,
        "Size": {"Ref": "DataVolumeGiB"},
        "VolumeType": "gp3",
        "SnapshotId": {"Fn::If": ["HasDataSnapshot", {"Ref": "DataSnapshotId"}, {"Ref": "AWS::NoValue"}]},
        "Tags": [
            {"Key": "Name", "Value": "epi-agent-phase2a-data"},
            {"Key": "Project", "Value": "epi-agent"},
            {"Key": "Environment", "Value": "phase2a"},
            {"Key": "Backup", "Value": "daily"},
        ],
    }
    bucket = resources["ApplicationBucket"]
    assert bucket["DeletionPolicy"] == "Retain"
    assert bucket["UpdateReplacePolicy"] == "Retain"
    properties = bucket["Properties"]
    assert properties["BucketEncryption"] == {
        "ServerSideEncryptionConfiguration": [
            {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
        ]
    }
    assert properties["VersioningConfiguration"] == {"Status": "Enabled"}
    assert properties["OwnershipControls"] == {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}
    assert properties["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    statement = resources["ApplicationBucketPolicy"]["Properties"]["PolicyDocument"]["Statement"]
    assert statement == [
        {
            "Sid": "DenyInsecureTransport",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:*",
            "Resource": [
                {"Fn::GetAtt": "ApplicationBucket.Arn"},
                {"Fn::Sub": "${ApplicationBucket.Arn}/*"},
            ],
            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
        }
    ]


def test_phase2a_cognito_is_invitation_only_without_a_client_secret() -> None:
    template = phase2a_template()
    resources = template["Resources"]
    types = Counter(resource["Type"] for resource in resources.values())

    assert types["AWS::Cognito::UserPool"] == 1
    assert types["AWS::Cognito::UserPoolClient"] == 1
    assert types["AWS::Cognito::UserPoolDomain"] == 1
    pool = resources["ApplicationUserPool"]
    assert pool["DeletionPolicy"] == "Retain"
    assert pool["UpdateReplacePolicy"] == "Retain"
    assert pool["Properties"]["UsernameAttributes"] == ["email"]
    assert pool["Properties"]["AutoVerifiedAttributes"] == ["email"]
    assert pool["Properties"]["UserPoolTier"] == "ESSENTIALS"
    assert pool["Properties"]["AdminCreateUserConfig"] == {"AllowAdminCreateUserOnly": True}
    assert pool["Properties"]["Policies"] == {
        "PasswordPolicy": {
            "MinimumLength": 14,
            "RequireLowercase": True,
            "RequireUppercase": True,
            "RequireNumbers": True,
            "RequireSymbols": True,
            "TemporaryPasswordValidityDays": 7,
        }
    }
    assert resources["ApplicationUserPoolClient"]["Properties"] == {
        "UserPoolId": {"Ref": "ApplicationUserPool"},
        "ClientName": "epi-agent-phase2a-public-app",
        "GenerateSecret": False,
        "AllowedOAuthFlowsUserPoolClient": True,
        "AllowedOAuthFlows": ["code"],
        "AllowedOAuthScopes": ["openid", "email"],
        "SupportedIdentityProviders": ["COGNITO"],
        "CallbackURLs": ["https://epiagent.org/auth/callback"],
        "LogoutURLs": ["https://epiagent.org/"],
    }


def test_phase2a_dns_uses_existing_zone_and_maps_apex_to_eip() -> None:
    template = phase2a_template()
    resources = template["Resources"]
    types = Counter(resource["Type"] for resource in resources.values())

    assert types["AWS::Route53::RecordSet"] == 1
    assert all("HostedZone" not in resource_type and "Domains" not in resource_type for resource_type in types)
    assert resources["ApplicationDnsRecord"]["Properties"] == {
        "HostedZoneId": {"Ref": "HostedZoneId"},
        "Name": {"Ref": "DomainName"},
        "Type": "A",
        "TTL": "300",
        "ResourceRecords": [{"Ref": "ApplicationElasticIp"}],
    }


def test_phase2a_parameters_examples_and_pinned_linter_contract() -> None:
    template = phase2a_template()

    assert set(template["Parameters"]) == {
        "DomainName",
        "HostedZoneId",
        "InstanceType",
        "RootVolumeGiB",
        "DataVolumeGiB",
        "CertificateEmail",
        "DataSnapshotId",
    }
    assert template["Conditions"] == {
        "HasDataSnapshot": {"Fn::Not": [{"Fn::Equals": [{"Ref": "DataSnapshotId"}, ""]}]}
    }
    assert template["Resources"]["ApplicationElasticIp"]["Properties"]["Tags"] == [
        {"Key": "Name", "Value": "epi-agent-phase2a"},
        {"Key": "Project", "Value": "epi-agent"},
        {"Key": "Environment", "Value": "phase2a"},
        {"Key": "CertificateContactEmail", "Value": {"Ref": "CertificateEmail"}},
    ]
    assert json.loads(PHASE2A_PARAMETERS.read_text(encoding="utf-8")) == [
        {"ParameterKey": "DomainName", "ParameterValue": "epiagent.org"},
        {"ParameterKey": "HostedZoneId", "ParameterValue": "Z02132461LVJ2PFOYFXFU"},
        {"ParameterKey": "InstanceType", "ParameterValue": "t3.large"},
        {"ParameterKey": "RootVolumeGiB", "ParameterValue": "30"},
        {"ParameterKey": "DataVolumeGiB", "ParameterValue": "50"},
    ]
    assert PHASE2A_LINT_REQUIREMENTS.read_text(encoding="utf-8") == "cfn-lint==1.53.1\n"
