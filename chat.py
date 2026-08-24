import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import boto3


TOOL_NAME = "create_bug_report"
GATEWAY_TOOL_NAME = "create-bug-report___create_bug_report"
DEFAULT_MODEL = "amazon.nova-pro-v1:0"
DEFAULT_REGION = "us-east-1"
DEFAULT_GATEWAY_URL = (
    "https://customer-support-bug-report-gateway-vtpdrz2yua."
    "gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
)


def call_gateway(gateway_url: str, arguments: dict[str, Any]) -> dict[str, Any]:
    body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "tools/call", "params": {"name": GATEWAY_TOOL_NAME, "arguments": arguments}}
    request = Request(gateway_url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    with urlopen(request, timeout=60) as response:
        payload = json.load(response)
    if "error" in payload:
        raise RuntimeError(f"Gateway error: {payload['error']}")
    return payload.get("result", {})


def tool_config() -> list[dict[str, Any]]:
    return [{"toolSpec": {"name": TOOL_NAME, "description": "Create a bug report after collecting all required fields.", "inputSchema": {"json": {"type": "object", "properties": {"description": {"type": "string"}, "stepsToReproduce": {"type": "string"}, "environment": {"type": "string"}}, "required": ["description", "stepsToReproduce", "environment"]}}}}]


def text_from_response(response: dict[str, Any]) -> str:
    return "".join(part.get("text", "") for part in response["output"]["message"].get("content", []))


def run_conversation(
    client: Any,
    turns: list[str],
    system_prompt: str,
    gateway_url: str,
    model_id: str,
    messages: list[dict[str, Any]] | None = None,
) -> str:
    if messages is None:
        messages = []
    last_response: dict[str, Any] = {}
    for turn in turns:
        messages.append({"role": "user", "content": [{"text": turn}]})
        response = client.converse(
            modelId=model_id,
            system=[{"text": system_prompt}],
            messages=messages,
            toolConfig={"tools": tool_config()},
        )
        messages.append(response["output"]["message"])
        if response.get("stopReason") == "tool_use":
            results = []
            for part in response["output"]["message"].get("content", []):
                if "toolUse" in part:
                    use = part["toolUse"]
                    result = call_gateway(gateway_url, use.get("input", {}))
                    results.append({"toolResult": {"toolUseId": use["toolUseId"], "content": [{"json": result}]}})
            messages.append({"role": "user", "content": results})
            response = client.converse(
                modelId=model_id,
                system=[{"text": system_prompt}],
                messages=messages,
                toolConfig={"tools": tool_config()},
            )
            messages.append(response["output"]["message"])
        last_response = response
    return text_from_response(last_response)


def main() -> int:
    config_path = Path(".harness/config.json")
    if not config_path.exists():
        raise RuntimeError("Harness is not created. Run: python create_harness.py")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    system_prompt = Path(config["systemPrompt"]).read_text(encoding="utf-8")
    client = boto3.client("bedrock-runtime", region_name=config.get("region", DEFAULT_REGION))
    gateway_url = os.getenv("AGENTCORE_GATEWAY_URL", config.get("gatewayUrl", DEFAULT_GATEWAY_URL))
    print("Customer support chat ready. Type 'quit' to exit.")
    messages: list[dict[str, Any]] = []
    for line in sys.stdin:
        message = line.strip()
        if not message:
            continue
        if message.lower() in {"quit", "exit"}:
            return 0
        print(run_conversation(client, [message], system_prompt, gateway_url, config.get("modelId", DEFAULT_MODEL), messages))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Chat error: {error}", file=sys.stderr)
        raise SystemExit(1)