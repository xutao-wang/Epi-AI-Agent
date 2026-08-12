"""Contract tests for the Phase 2A AWS infrastructure templates."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import yaml

from scripts.smoke_aws_phase2a_template_regressions import assert_release_archive_extractor_works


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
        "ManageEpiAgentSsmDocuments",
        "ManageEpiAgentInstanceProfiles",
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


def test_bootstrap_execution_policy_manages_the_complete_document_lifecycle() -> None:
    statements = bootstrap_template()["Resources"]["CloudFormationExecutionPolicy"]["Properties"][
        "PolicyDocument"
    ]["Statement"]
    documents = next(
        statement for statement in statements if statement["Sid"] == "ManageEpiAgentSsmDocuments"
    )

    assert documents["Action"] == [
        "ssm:CreateDocument",
        "ssm:UpdateDocument",
        "ssm:UpdateDocumentDefaultVersion",
        "ssm:DeleteDocument",
        "ssm:DescribeDocument",
        "ssm:GetDocument",
        "ssm:AddTagsToResource",
        "ssm:RemoveTagsFromResource",
        "ssm:ListTagsForResource",
    ]
    assert documents["Resource"] == {
        "Fn::Sub": "arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:document/epi-agent-*"
    }


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
    assert all(action.split(":", 1)[0] in {"cloudwatch", "ec2", "ec2messages", "logs", "s3", "ssm", "ssmmessages"} for action in actions)
    assert all(not action.startswith(("iam:", "sts:")) for action in actions)


def test_bootstrap_stack_scopes_route53_change_lookup_to_change_arns() -> None:
    statements = bootstrap_template()["Resources"]["CloudFormationExecutionPolicy"]["Properties"][
        "PolicyDocument"
    ]["Statement"]

    records = next(statement for statement in statements if statement["Sid"] == "ManageDnsRecords")
    changes = next(statement for statement in statements if statement["Sid"] == "ReadDnsChanges")
    assert records["Action"] == [
        "route53:ChangeResourceRecordSets",
        "route53:GetHostedZone",
        "route53:ListResourceRecordSets",
    ]
    assert records["Resource"] == {"Fn::Sub": "arn:${AWS::Partition}:route53:::hostedzone/*"}
    assert changes == {
        "Sid": "ReadDnsChanges",
        "Effect": "Allow",
        "Action": "route53:GetChange",
        "Resource": {"Fn::Sub": "arn:${AWS::Partition}:route53:::change/*"},
    }


def test_bootstrap_policy_supports_phase2a_endpoint_storage_and_cognito_domain_lifecycle() -> None:
    statements = bootstrap_template()["Resources"]["CloudFormationExecutionPolicy"]["Properties"][
        "PolicyDocument"
    ]["Statement"]
    actions = {
        action
        for statement in statements
        for action in ([statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"])
    }

    assert {
        "ec2:CreateVpcEndpoint",
        "ec2:DeleteVpcEndpoints",
        "ec2:DescribeVpcEndpoints",
        "ec2:ModifyVpcEndpoint",
        "ec2:AssociateRouteTable",
        "ec2:DisassociateRouteTable",
        "ec2:CreateTags",
        "ec2:DeleteTags",
        "s3:GetBucketVersioning",
        "s3:PutBucketVersioning",
        "s3:GetBucketOwnershipControls",
        "s3:PutBucketOwnershipControls",
        "s3:GetBucketPublicAccessBlock",
        "s3:PutBucketPublicAccessBlock",
        "s3:GetBucketTagging",
        "s3:PutBucketTagging",
        "cognito-idp:CreateUserPoolDomain",
        "cognito-idp:DeleteUserPoolDomain",
        "cognito-idp:DescribeUserPoolDomain",
        "cognito-idp:UpdateUserPoolDomain",
    } <= actions
    assert "s3:DeleteBucketTagging" not in actions


def test_bootstrap_policy_supports_cognito_resource_tag_lifecycle() -> None:
    statements = bootstrap_template()["Resources"]["CloudFormationExecutionPolicy"]["Properties"][
        "PolicyDocument"
    ]["Statement"]
    cognito = next(statement for statement in statements if statement["Sid"] == "ManageCognito")

    assert {"cognito-idp:TagResource", "cognito-idp:UntagResource"} <= set(cognito["Action"])


def test_bootstrap_boundary_and_execution_role_support_task7_without_broadening_iam() -> None:
    template = bootstrap_template()
    boundary = template["Resources"]["EpiAgentWorkloadBoundary"]["Properties"]["PolicyDocument"]["Statement"]
    boundary_actions = {action for statement in boundary for action in ([statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"])}
    assert {"ssm:DescribeAssociation", "ssm:ListInstanceAssociations", "ssm:UpdateInstanceAssociationStatus", "ssm:GetDocument", "ssm:GetDeployablePatchSnapshotForInstance", "ssm:PutInventory", "ssm:PutComplianceItems", "ssm:PutConfigurePackageResult", "ssm:UpdateAssociationStatus", "ssmmessages:OpenDataChannel", "ec2messages:AcknowledgeMessage", "cloudwatch:PutMetricData", "ec2:CreateSnapshot", "ec2:DescribeVolumes"} <= boundary_actions
    execution = template["Resources"]["CloudFormationExecutionPolicy"]["Properties"]["PolicyDocument"]["Statement"]
    execution_actions = {action for statement in execution for action in ([statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"])}
    assert {"iam:CreateInstanceProfile", "iam:AddRoleToInstanceProfile", "ssm:CreateDocument", "ssm:UpdateDocument", "logs:PutMetricFilter", "ec2:ModifyInstanceAttribute"} <= execution_actions
    log_statements = {statement["Sid"]: statement for statement in boundary if statement["Sid"] in {"WriteWorkloadLogs", "DescribeWorkloadLogStreams"}}
    assert log_statements["WriteWorkloadLogs"]["Resource"] == {"Fn::Sub": "arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/epi-agent/*:*"}
    assert log_statements["DescribeWorkloadLogStreams"] == {"Sid": "DescribeWorkloadLogStreams", "Effect": "Allow", "Action": "logs:DescribeLogStreams", "Resource": {"Fn::Sub": "arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/epi-agent/*"}}


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
    bucket_policy = resources["ApplicationBucketPolicy"]
    assert bucket_policy["DeletionPolicy"] == "Retain"
    assert bucket_policy["UpdateReplacePolicy"] == "Retain"
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
        "AlertEmail",
        "DataSnapshotId",
    }
    assert template["Conditions"] == {
        "HasDataSnapshot": {"Fn::Not": [{"Fn::Equals": [{"Ref": "DataSnapshotId"}, ""]}]},
        "HasAlertEmail": {"Fn::Not": [{"Fn::Equals": [{"Ref": "AlertEmail"}, ""]}]},
    }
    assert template["Parameters"]["DataSnapshotId"]["Description"] == (
        "Optional EBS snapshot ID used only when creating the data volume. "
        "Changing it in-place is unsupported; use a separate restore/new-volume migration workflow."
    )
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


def test_phase2a_compute_uses_a_hardened_single_worker_with_retained_data() -> None:
    template = phase2a_template()
    resources = template["Resources"]
    instance = resources["ApplicationInstance"]
    properties = instance["Properties"]

    assert instance["Type"] == "AWS::EC2::Instance"
    assert properties["InstanceType"] == "t3.large"
    assert properties["CreditSpecification"] == {"CPUCredits": "standard"}
    assert properties["ImageId"] == "{{resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64}}"
    assert properties["DisableApiTermination"] is True
    assert properties["MetadataOptions"] == {
        "HttpEndpoint": "enabled",
        "HttpTokens": "required",
        "HttpPutResponseHopLimit": 1,
        "InstanceMetadataTags": "disabled",
    }
    assert "KeyName" not in properties
    assert properties["BlockDeviceMappings"] == [
        {
            "DeviceName": "/dev/xvda",
            "Ebs": {
                "DeleteOnTermination": True,
                "Encrypted": True,
                "VolumeSize": 30,
                "VolumeType": "gp3",
            },
        }
    ]
    attachment = resources["ApplicationDataVolumeAttachment"]
    assert attachment["Type"] == "AWS::EC2::VolumeAttachment"
    assert attachment["Properties"] == {
        "Device": "/dev/sdf",
        "InstanceId": {"Ref": "ApplicationInstance"},
        "VolumeId": {"Ref": "ApplicationDataVolume"},
    }
    assert resources["ApplicationElasticIpAssociation"]["Properties"] == {
        "AllocationId": {"Fn::GetAtt": "ApplicationElasticIp.AllocationId"},
        "InstanceId": {"Ref": "ApplicationInstance"},
    }


def test_phase2a_instance_iam_is_read_only_for_artifacts_and_observability() -> None:
    resources = phase2a_template()["Resources"]
    role = resources["ApplicationInstanceRole"]["Properties"]
    assert role["ManagedPolicyArns"] == [
        {"Fn::Sub": "arn:${AWS::Partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"}
    ]
    statements = role["Policies"][0]["PolicyDocument"]["Statement"]
    allowed_actions = {
        action
        for statement in statements
        for action in ([statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"])
    }
    assert {"s3:GetObject", "s3:GetObjectVersion", "ssm:GetParameter", "logs:PutLogEvents", "cloudwatch:PutMetricData"} <= allowed_actions
    assert all(not action.startswith(("s3:Put", "route53:", "cognito-idp:", "iam:")) for action in allowed_actions)
    artifact_statement = next(statement for statement in statements if statement["Sid"] == "ReadReleaseAndStudyArtifacts")
    assert artifact_statement["Resource"] == [
        {"Fn::Sub": "${ApplicationBucket.Arn}/releases/*"},
        {"Fn::Sub": "${ApplicationBucket.Arn}/studies/*"},
    ]


def test_phase2a_userdata_is_guarded_idempotent_and_defers_application_start() -> None:
    properties = phase2a_template()["Resources"]["ApplicationInstance"]["Properties"]
    user_data = properties["UserData"]["Fn::Base64"]["Fn::Sub"]

    for package in (
        "awscli-2", "amazon-cloudwatch-agent", "nginx", "certbot", "python3-certbot-nginx", "acl", "jq", "sudo", "uv"
    ):
        assert package in user_data
    assert "expected_volume_id='${ApplicationDataVolume}'" in user_data
    assert "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol" in user_data
    assert "blkid" in user_data
    assert "mkfs" in user_data
    assert "UUID=" in user_data
    assert "epi-agent-web" in user_data
    assert "epi-agent-exec" in user_data
    assert "REPORT_AGENT_COGNITO_USER_POOL_ID=${ApplicationUserPool}" in user_data
    assert "REPORT_AGENT_CORS_ALLOW_ORIGIN_REGEX=^https://$domain_regex$" in user_data
    assert "SECRET" not in user_data
    assert "python3.12" in user_data
    assert "uv==0.6.14" in user_data
    assert "epi-agent-bootstrap.conf" in user_data
    assert "location ^~ /.well-known/acme-challenge/" in user_data
    assert "systemctl disable --now epi-agent.service" in user_data
    assert "cfn-signal" in user_data
    assert "rm -rf /srv/epi-agent" not in user_data
    assert user_data.count("${ApplicationDataVolume}") == 1


def test_phase2a_ssm_release_document_uses_strict_environment_interpolation() -> None:
    document = phase2a_template()["Resources"]["EpiAgentDeployReleaseDocument"]["Properties"]
    content = document["Content"]

    assert document["DocumentType"] == "Command"
    assert content["schemaVersion"] == "2.2"
    assert content["mainSteps"][0]["action"] == "aws:runShellScript"
    inputs = content["mainSteps"][0]["inputs"]
    parameters = content["parameters"]
    parameter_names = (
        "Bucket",
        "ReleaseKey",
        "ReleaseSha256",
        "ReleaseId",
        "DomainName",
        "CertificateEmail",
    )
    assert "interpolationType" not in inputs
    for name in parameter_names:
        assert parameters[name]["interpolationType"] == "ENV_VAR"
        assert parameters[name]["allowedPattern"].startswith("^")
    assert "{{ Bucket }}" not in "\n".join(inputs["runCommand"])
    command = "\n".join(inputs["runCommand"])
    assert "install-release.sh" in command
    assert "install -o root -g root -m 0750" in command
    assert "eval " not in command
    assert "bash -c" not in command
    assert 'sed "s/epiagent\\\\.org/$SSM_DomainName/g"' in command
    assert "/etc/nginx/staged/epi-agent.conf" in command
    assert "/etc/nginx/conf.d/epi-agent.conf" not in command


def test_phase2a_ssm_release_archive_extractor_runs_before_tarfile_closes() -> None:
    assert_release_archive_extractor_works(phase2a_template())


def test_phase2a_ssm_release_document_patterns_are_re2_compatible() -> None:
    parameters = phase2a_template()["Resources"]["EpiAgentDeployReleaseDocument"]["Properties"][
        "Content"
    ]["parameters"]
    patterns = {name: parameter["allowedPattern"] for name, parameter in parameters.items()}

    for pattern in patterns.values():
        assert all(token not in pattern for token in ("(?=", "(?!", "(?<=", "(?<!"))

    release_key = re.compile(patterns["ReleaseKey"])
    assert release_key.fullmatch("releases/0123456789abcdef.tar.gz")
    assert not release_key.fullmatch("releases/../secrets")
    assert not release_key.fullmatch("releases/package..tar.gz")
    assert not release_key.fullmatch("releases//package.tar.gz")

    domain_name = re.compile(patterns["DomainName"])
    assert domain_name.fullmatch("epiagent.org")
    assert not domain_name.fullmatch("-epiagent.org")
    assert not domain_name.fullmatch("epiagent-.org")


def test_phase2a_backup_and_observability_cover_host_failure_modes() -> None:
    template = phase2a_template()
    resources = template["Resources"]

    lifecycle = resources["ApplicationDataVolumeLifecyclePolicy"]["Properties"]
    assert re.fullmatch(r"[0-9A-Za-z _-]+", lifecycle["Description"])
    assert lifecycle["State"] == "ENABLED"
    schedule = lifecycle["PolicyDetails"]["Schedules"][0]
    assert schedule["CreateRule"]["Interval"] == 24
    assert schedule["CreateRule"]["IntervalUnit"] == "HOURS"
    assert schedule["RetainRule"] == {"Count": 14}
    assert resources["ApplicationLogGroup"]["Properties"]["RetentionInDays"] == 30
    assert resources["ApplicationAlertTopic"]["Type"] == "AWS::SNS::Topic"
    assert resources["ApplicationAlertEmailSubscription"]["Condition"] == "HasAlertEmail"
    assert template["Conditions"]["HasAlertEmail"] == {"Fn::Not": [{"Fn::Equals": [{"Ref": "AlertEmail"}, ""]}]}

    alarm_resources = [resource for resource in resources.values() if resource["Type"] == "AWS::CloudWatch::Alarm"]
    alarm_names = {alarm["Properties"]["AlarmName"] for alarm in alarm_resources}
    assert {"epi-agent-ec2-status", "epi-agent-cpu", "epi-agent-cpu-credits", "epi-agent-memory", "epi-agent-data-disk", "epi-agent-service-errors"} <= alarm_names
    parameter = resources["CloudWatchAgentConfiguration"]["Properties"]
    assert parameter["Type"] == "String"
    configuration = parameter["Value"]["Fn::Sub"]
    assert "${ApplicationLogGroup}" in configuration
    assert "/var/log/epi-agent/application.log" in configuration
    assert "mem_used_percent" in configuration
    assert "inodes_free" in configuration
    assert '"drop_device": true' in configuration
    assert "/srv/epi-agent" in configuration
    assert resources["EpiAgentDeployReleaseDocument"]["Properties"]["UpdateMethod"] == "NewVersion"
    assert template["Outputs"]["ApplicationInstanceId"]["Value"] == {"Ref": "ApplicationInstance"}
    assert template["Outputs"]["DeployReleaseDocumentName"]["Value"] == {"Ref": "EpiAgentDeployReleaseDocument"}


def test_phase2a_recovery_document_is_fixed_and_parameter_free() -> None:
    template = phase2a_template()
    document = template["Resources"]["EpiAgentRecoverStudyAccessDocument"]

    assert document["Type"] == "AWS::SSM::Document"
    properties = document["Properties"]
    assert properties["Name"] == "epi-agent-recover-study-access"
    assert properties["DocumentType"] == "Command"
    assert properties["DocumentFormat"] == "YAML"
    assert properties["UpdateMethod"] == "NewVersion"
    content = properties["Content"]
    assert "parameters" not in content
    assert content["schemaVersion"] == "2.2"
    step = content["mainSteps"][0]
    assert step["action"] == "aws:runShellScript"
    command = "\n".join(step["inputs"]["runCommand"])
    ownership = (
        "chown -R -h epi-agent-web:epi-agent-web "
        '"$study_root"'
    )
    privilege_drop = (
        "/usr/sbin/runuser --user epi-agent-web -- /usr/bin/env -i"
    )
    runtime_install = (
        "install -d -m 0750 -o epi-agent-web -g epi-agent-web "
        "/run/epi-agent"
    )
    assert "readonly study_root=/srv/epi-agent/study_data" in command
    assert command.startswith("exec /usr/bin/bash -Eeuo pipefail <<'BASH'\n")
    assert command.rstrip().endswith("BASH")
    assert ownership in command
    assert privilege_drop in command
    assert "PATH=/opt/epi-agent/current/.venv/bin:/usr/bin" in command
    assert "LANG=C.UTF-8" in command
    assert "LC_ALL=C.UTF-8" in command
    assert "PYTHONUTF8=1" in command
    assert "REPORT_AGENT_STUDY_ROOT=/srv/epi-agent/study_data" in command
    assert runtime_install in command
    assert command.index(ownership) < command.index(privilege_drop)
    assert command.index("active study index is not readable") < command.index(
        runtime_install
    )
    assert command.index(runtime_install) < command.index(
        "systemctl restart epi-agent.service"
    )
    assert command.index("systemctl restart epi-agent.service") < command.index(
        "http://127.0.0.1:8000/api/health"
    )
    assert "OPENAI_API_KEY" not in command
    assert "AWS_ACCESS_KEY_ID" not in command
    assert "eval " not in command
    assert "bash -c" not in command
    assert "rm -rf" not in command
    assert "remaining_seconds=$((recovery_deadline - SECONDS))" in command
    assert "--max-time \"$request_timeout\"" in command
    assert template["Outputs"]["RecoverStudyAccessDocumentName"]["Value"] == {
        "Ref": "EpiAgentRecoverStudyAccessDocument"
    }
