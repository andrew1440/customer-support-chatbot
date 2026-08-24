import argparse
import json
import sys
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError


GATEWAY_NAME = "customer-support-bug-report-gateway"
GATEWAY_ROLE_NAME = "customer-support-agentcore-gateway-role"
TARGET_NAME = "create_bug_report"
TARGET_RESOURCE_NAME = "create-bug-report"


def find_gateway(client: Any, name: str) -> dict[str, Any] | None:
    response = client.list_gateways()
    for gateway in response.get("items", []):
        if gateway.get("name") == name:
            return gateway
    return None


def find_target(client: Any, gateway_id: str, name: str) -> dict[str, Any] | None:
    response = client.list_gateway_targets(gatewayIdentifier=gateway_id)
    for target in response.get("items", []):
        if target.get("name") == name:
            return target
    return None


def ensure_gateway_role(iam: Any, account_id: str, region: str, lambda_arn: str) -> str:
    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "lambda:InvokeFunction",
                "Resource": lambda_arn,
            }
        ],
    }
    try:
        role = iam.get_role(RoleName=GATEWAY_ROLE_NAME)["Role"]
    except iam.exceptions.NoSuchEntityException:
        role = iam.create_role(
            RoleName=GATEWAY_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(assume_role_policy),
            Description="Allows AgentCore Gateway to invoke the bug report Lambda",
        )["Role"]
        time.sleep(10)

    iam.put_role_policy(
        RoleName=GATEWAY_ROLE_NAME,
        PolicyName="InvokeBugReportLambda",
        PolicyDocument=json.dumps(policy),
    )
    return role["Arn"]


def tool_schema() -> dict[str, Any]:
    return {
        "inlinePayload": [
            {
                "name": TARGET_NAME,
                "description": (
                    "Create a customer bug report after collecting a description, "
                    "steps to reproduce, and the customer's environment."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "description": {
                            "type": "string",
                            "description": "What went wrong.",
                        },
                        "stepsToReproduce": {
                            "type": "string",
                            "description": "The steps that reproduce the problem.",
                        },
                        "environment": {
                            "type": "string",
                            "description": "Browser, operating system, and device details.",
                        },
                    },
                    "required": ["description", "stepsToReproduce", "environment"],
                },
            }
        ]
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--stack-name", default="bug-report-tool-stack")
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    account_id = session.client("sts").get_caller_identity()["Account"]
    cloudformation = session.client("cloudformation")
    outputs = cloudformation.describe_stacks(StackName=args.stack_name)["Stacks"][0].get("Outputs", [])
    output_map = {item["OutputKey"]: item["OutputValue"] for item in outputs}
    lambda_arn = output_map.get("LambdaFunctionArn")
    if not lambda_arn:
        raise RuntimeError(f"Stack {args.stack_name!r} has no LambdaFunctionArn output")

    iam = session.client("iam")
    role_arn = ensure_gateway_role(iam, account_id, args.region, lambda_arn)
    control = session.client("bedrock-agentcore-control")

    gateway = find_gateway(control, GATEWAY_NAME)
    if gateway:
        gateway_id = gateway["gatewayId"]
        print(f"Reusing gateway: {gateway_id}")
    else:
        gateway = control.create_gateway(
            name=GATEWAY_NAME,
            description="MCP gateway for the customer support bug-report tool",
            roleArn=role_arn,
            protocolType="MCP",
            protocolConfiguration={
                "mcp": {"supportedVersions": ["2025-03-26"]}
            },
            authorizerType="NONE",
        )
        gateway_id = gateway["gatewayId"]
        print(f"Created gateway: {gateway_id}")

    target = find_target(control, gateway_id, TARGET_NAME)
    if target:
        target_id = target["targetId"]
        print(f"Reusing target: {target_id}")
    else:
        target = control.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=TARGET_RESOURCE_NAME,
            description="Lambda target that stores customer bug reports",
            targetConfiguration={
                "mcp": {"lambda": {"lambdaArn": lambda_arn, "toolSchema": tool_schema()}}
            },
            credentialProviderConfigurations=[
                {"credentialProviderType": "GATEWAY_IAM_ROLE"}
            ],
        )
        target_id = target["targetId"]
        print(f"Created target: {target_id}")

    print(json.dumps({"gatewayId": gateway_id, "targetId": target_id, "lambdaArn": lambda_arn}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ClientError as error:
        print(f"AWS error: {error}", file=sys.stderr)
        raise
