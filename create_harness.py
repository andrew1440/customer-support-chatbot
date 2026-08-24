import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import boto3


DEFAULT_MODEL = "amazon.nova-pro-v1:0"
DEFAULT_REGION = "us-east-1"
DEFAULT_GATEWAY_URL = (
    "https://customer-support-bug-report-gateway-vtpdrz2yua."
    "gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
)
TOOL_NAME = "create_bug_report"


def load_system_prompt(path: str) -> str:
    with open(path, encoding="utf-8") as prompt_file:
        return prompt_file.read()


def build_system_prompt(prompt_path: str, faq_path: str) -> str:
    system_prompt = load_system_prompt(prompt_path)
    with open(faq_path, encoding="utf-8") as faq_file:
        faq = faq_file.read()
    return system_prompt.replace("{{FAQ}}", faq)


def tool_config() -> list[dict[str, Any]]:
    return [
        {
            "toolSpec": {
                "name": TOOL_NAME,
                "description": (
                    "Create a bug report after collecting a description, steps to "
                    "reproduce, and the customer environment."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "stepsToReproduce": {"type": "string"},
                            "environment": {"type": "string"},
                        },
                        "required": [
                            "description",
                            "stepsToReproduce",
                            "environment",
                        ],
                    }
                },
            }
        }
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or update the local customer support harness.")
    parser.add_argument("--region", default=os.getenv("AWS_REGION", DEFAULT_REGION))
    parser.add_argument("--model-id", default=os.getenv("BEDROCK_MODEL_ID", DEFAULT_MODEL))
    parser.add_argument("--system-prompt", default="system_prompt.txt")
    parser.add_argument("--faq", default="online_shop_faq.md")
    parser.add_argument("--output", default=".harness/system_prompt.txt")
    args = parser.parse_args()

    system_prompt = build_system_prompt(args.system_prompt, args.faq)
    if "{{FAQ}}" in system_prompt:
        raise RuntimeError("FAQ placeholder was not replaced")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(system_prompt, encoding="utf-8")
    config = {
        "region": args.region,
        "modelId": args.model_id,
        "gatewayUrl": os.getenv("AGENTCORE_GATEWAY_URL", DEFAULT_GATEWAY_URL),
        "systemPrompt": str(output_path),
        "tool": TOOL_NAME,
    }
    config_path = output_path.with_name("config.json")
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"Harness ready: {output_path}")
    print(f"Configuration: {config_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
