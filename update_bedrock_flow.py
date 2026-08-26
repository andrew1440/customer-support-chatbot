#!/usr/bin/env python3
import argparse
import json
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

import boto3


DEFAULT_REGION = "us-east-1"
DEFAULT_FLOW_ID = "5DCGJQ508A"
DEFAULT_FLOW_ALIAS_ID = "PFESM2MECD"


INLINE_CODE = r'''import json
import re

text = "" if variable is None else str(variable).strip()

status = "INCOMPLETE"
payload = {}
message = text


def clean_message(value):
    value = value.strip()
    value = re.split(r"\n\s*---", value, maxsplit=1)[0].strip()
    value = re.sub(
        r"\n?\s*\*\*WAITING FOR CUSTOMER'?S RESPONSE\*\*",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()
    return value


if "COMPLETE:" in text:
    after_marker = text.split("COMPLETE:", 1)[1].strip()
    try:
        decoded, _ = json.JSONDecoder().raw_decode(after_marker)
        if isinstance(decoded, dict):
            payload = {
                "description": str(decoded.get("description", "")).strip(),
                "stepsToReproduce": str(decoded.get("stepsToReproduce", "")).strip(),
                "environment": str(decoded.get("environment", "")).strip(),
            }
            if all(payload.values()):
                status = "COMPLETE"
                message = ""
    except Exception:
        status = "INCOMPLETE"

if status != "COMPLETE":
    ask_match = re.search(r"ASK:\s*(.*)", text, re.DOTALL | re.IGNORECASE)
    if ask_match:
        message = clean_message(ask_match.group(1))
    else:
        message = clean_message(re.sub(
            r"^THOUGHT:\s*.*?\n",
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        ))
    if not message:
        message = "Could you share a little more detail about what happened?"

response = {
    "status": status,
    "payload": payload,
    "message": message,
}
response
'''


def load_classifier_prompt(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if "{{customer_message}}" not in text:
        raise ValueError(f"{path} does not contain {{customer_message}}")
    return text


def load_faq_prompt(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if "{{customer_message}}" not in text:
        raise ValueError(f"{path} does not contain {{customer_message}}")
    if "[Full contents of online_shop_faq.md" in text:
        raise ValueError(f"{path} still contains the FAQ placeholder")
    return text


def find_node(definition: dict[str, Any], name: str) -> dict[str, Any]:
    for node in definition["nodes"]:
        if node["name"] == name:
            return node
    raise ValueError(f"Could not find flow node {name!r}")


def set_input(
    node: dict[str, Any],
    name: str,
    data_type: str,
    expression: str,
) -> None:
    for item in node.setdefault("inputs", []):
        if item["name"] == name:
            item["type"] = data_type
            item["expression"] = expression
            return
    node.setdefault("inputs", []).append(
        {"name": name, "type": data_type, "expression": expression}
    )


def set_single_output(node: dict[str, Any], name: str, data_type: str) -> None:
    node["outputs"] = [{"name": name, "type": data_type}]


def data_connection(
    name: str,
    source: str,
    target: str,
    source_output: str,
    target_input: str,
) -> dict[str, Any]:
    return {
        "type": "Data",
        "name": name,
        "source": source,
        "target": target,
        "configuration": {
            "data": {
                "sourceOutput": source_output,
                "targetInput": target_input,
            }
        },
    }


def conditional_connection(
    name: str,
    source: str,
    target: str,
    condition: str,
) -> dict[str, Any]:
    return {
        "type": "Conditional",
        "name": name,
        "source": source,
        "target": target,
        "configuration": {"conditional": {"condition": condition}},
    }


def condition_name(connection: dict[str, Any]) -> str | None:
    return (
        connection.get("configuration", {})
        .get("conditional", {})
        .get("condition")
    )


def target_input(connection: dict[str, Any]) -> str | None:
    return connection.get("configuration", {}).get("data", {}).get("targetInput")


def should_replace_connection(connection: dict[str, Any]) -> bool:
    source = connection["source"]
    target = connection["target"]
    conn_type = connection["type"]

    if target == "ClassifierPrompt" and target_input(connection) == "topic":
        return True

    if source == "InlineCodeNode_1" and target in {
        "isrequestcomplete",
        "CreatebugReport",
        "BugReportFollowup",
    }:
        return True

    if source == "CreatebugReport" and target in {
        "isrequestcomplete",
        "BugReportFollowup",
    }:
        return True

    if source == "isrequestcomplete" and target in {
        "CreatebugReport",
        "BugReportFollowup",
    }:
        return True

    if (
        conn_type == "Conditional"
        and source == "ConditionNode_1"
        and target == "OtherRequest"
        and condition_name(connection) in {"isOtherrequest", "default"}
    ):
        return True

    return False


def build_updated_definition(
    flow: dict[str, Any],
    classifier_prompt: str,
    faq_prompt: str,
) -> dict[str, Any]:
    definition = deepcopy(flow["definition"])

    classifier = find_node(definition, "ClassifierPrompt")
    classifier_inline = classifier["configuration"]["prompt"]["sourceConfiguration"][
        "inline"
    ]
    classifier_text = classifier_inline["templateConfiguration"]["text"]
    classifier_text["text"] = classifier_prompt
    classifier_text["inputVariables"] = [{"name": "customer_message"}]
    classifier["inputs"] = [
        {"name": "customer_message", "type": "String", "expression": "$.data"}
    ]
    classifier_inference = classifier_inline.setdefault(
        "inferenceConfiguration", {}
    ).setdefault("text", {})
    classifier_inference["temperature"] = 0
    classifier_inference["maxTokens"] = 16

    faq = find_node(definition, "FAQPrompt")
    faq_inline = faq["configuration"]["prompt"]["sourceConfiguration"]["inline"]
    faq_text = faq_inline["templateConfiguration"]["text"]
    faq_text["text"] = faq_prompt
    faq_text["inputVariables"] = [{"name": "customer_message"}]
    faq["inputs"] = [
        {"name": "customer_message", "type": "String", "expression": "$.data"}
    ]
    faq_inference = faq_inline.setdefault("inferenceConfiguration", {}).setdefault(
        "text", {}
    )
    faq_inference["temperature"] = 0

    route_by_category = find_node(definition, "ConditionNode_1")
    route_by_category["configuration"]["condition"]["conditions"] = [
        {"name": "isBugReport", "expression": 'conditioninput == "BUG_REPORT"'},
        {
            "name": "isPlatformQuestion",
            "expression": 'conditioninput == "PLATFORM_QUESTION"',
        },
        {"name": "default"},
    ]

    inline_code = find_node(definition, "InlineCodeNode_1")
    inline_code["configuration"]["inlineCode"]["code"] = INLINE_CODE
    set_single_output(inline_code, "response", "Object")

    is_complete = find_node(definition, "isrequestcomplete")
    set_input(is_complete, "conditionInput", "String", "$.data.status")
    is_complete["configuration"]["condition"]["conditions"] = [
        {"name": "isComplete", "expression": 'conditionInput == "COMPLETE"'},
        {"name": "default"},
    ]

    create_bug = find_node(definition, "CreatebugReport")
    set_input(create_bug, "codeHookInput", "Object", "$.data.payload")
    set_single_output(create_bug, "functionResponse", "Object")

    confirmation = find_node(definition, "BugReportConfirmation")
    set_input(confirmation, "ticket_info", "Object", "$.data")

    followup = find_node(definition, "BugReportFollowup")
    set_input(followup, "document", "String", "$.data.message")

    definition["connections"] = [
        connection
        for connection in definition["connections"]
        if not should_replace_connection(connection)
    ]
    definition["connections"].extend(
        [
            data_connection(
                "InlineCodeNode_1ToIsRequestCompleteData",
                "InlineCodeNode_1",
                "isrequestcomplete",
                "response",
                "conditionInput",
            ),
            data_connection(
                "InlineCodeNode_1ToCreateBugReportData",
                "InlineCodeNode_1",
                "CreatebugReport",
                "response",
                "codeHookInput",
            ),
            data_connection(
                "InlineCodeNode_1ToBugReportFollowupData",
                "InlineCodeNode_1",
                "BugReportFollowup",
                "response",
                "document",
            ),
            conditional_connection(
                "IsRequestCompleteToCreateBugReportConditional",
                "isrequestcomplete",
                "CreatebugReport",
                "isComplete",
            ),
            conditional_connection(
                "IsRequestCompleteToBugReportFollowupConditional",
                "isrequestcomplete",
                "BugReportFollowup",
                "default",
            ),
            conditional_connection(
                "ConditionNode_1ToOtherRequestDefaultConditional",
                "ConditionNode_1",
                "OtherRequest",
                "default",
            ),
        ]
    )

    return definition


def summarize_definition(definition: dict[str, Any]) -> None:
    interesting = {
        "ClassifierPrompt",
        "FAQPrompt",
        "ConditionNode_1",
        "InlineCodeNode_1",
        "isrequestcomplete",
        "CreatebugReport",
        "BugReportConfirmation",
        "BugReportFollowup",
    }
    for node in definition["nodes"]:
        if node["name"] in interesting:
            print(f"{node['name']} ({node['type']})")
            print(f"  inputs: {node.get('inputs', [])}")
            print(f"  outputs: {node.get('outputs', [])}")
            if node["type"] == "Condition":
                conditions = node["configuration"]["condition"]["conditions"]
                print(f"  conditions: {conditions}")

    print("\nConnections:")
    for connection in definition["connections"]:
        if connection["source"] in interesting or connection["target"] in interesting:
            print(
                f"  {connection['source']} -> {connection['target']}"
                f" [{connection['type']}] {connection.get('configuration', {})}"
            )


def wait_until_prepared(client: Any, flow_id: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        flow = client.get_flow(flowIdentifier=flow_id)
        status = flow["status"]
        print(f"Flow status: {status}")
        if status == "Prepared":
            return
        if status == "Failed":
            print(json.dumps(flow.get("validations", []), indent=2, default=str))
            raise RuntimeError("Flow preparation failed")
        if time.monotonic() >= deadline:
            raise TimeoutError("Timed out waiting for the flow to prepare")
        time.sleep(5)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Patch and publish the customer-support-chatbot Bedrock Flow."
    )
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--flow-id", default=DEFAULT_FLOW_ID)
    parser.add_argument("--flow-alias-id", default=DEFAULT_FLOW_ALIAS_ID)
    parser.add_argument(
        "--classifier-prompt",
        default="Prompts/classifier-prompt-config.txt",
    )
    parser.add_argument(
        "--faq-prompt",
        default="Prompts/FAQ-prompt.txt",
    )
    parser.add_argument(
        "--from-json",
        help="Patch a local get_flow JSON file instead of fetching the live flow.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print the updated node/connection summary.",
    )
    parser.add_argument(
        "--out-json",
        help="Optional path to write the patched flow definition JSON.",
    )
    parser.add_argument("--prepare-timeout", type=int, default=180)
    args = parser.parse_args()

    classifier_prompt = load_classifier_prompt(Path(args.classifier_prompt))
    faq_prompt = load_faq_prompt(Path(args.faq_prompt))

    if args.from_json:
        flow = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        alias = None
        client = None
    else:
        client = boto3.client("bedrock-agent", region_name=args.region)
        flow = client.get_flow(flowIdentifier=args.flow_id)
        alias = client.get_flow_alias(
            flowIdentifier=args.flow_id,
            aliasIdentifier=args.flow_alias_id,
        )

    definition = build_updated_definition(flow, classifier_prompt, faq_prompt)

    if args.out_json:
        Path(args.out_json).write_text(
            json.dumps(definition, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    if args.dry_run:
        summarize_definition(definition)
        return 0

    if client is None or alias is None:
        raise ValueError("--from-json can only be used with --dry-run")

    update_params = {
        "flowIdentifier": args.flow_id,
        "name": flow["name"],
        "executionRoleArn": flow["executionRoleArn"],
        "definition": definition,
    }
    if flow.get("description"):
        update_params["description"] = flow["description"]
    if flow.get("customerEncryptionKeyArn"):
        update_params["customerEncryptionKeyArn"] = flow["customerEncryptionKeyArn"]

    print(f"Updating draft flow {args.flow_id}...")
    client.update_flow(**update_params)

    print("Preparing flow...")
    client.prepare_flow(flowIdentifier=args.flow_id)
    wait_until_prepared(client, args.flow_id, args.prepare_timeout)

    print("Creating flow version...")
    version = client.create_flow_version(flowIdentifier=args.flow_id)["version"]
    print(f"Created flow version {version}")

    alias_params = {
        "flowIdentifier": args.flow_id,
        "aliasIdentifier": args.flow_alias_id,
        "name": alias["name"],
        "routingConfiguration": [{"flowVersion": version}],
    }
    if alias.get("description"):
        alias_params["description"] = alias["description"]
    if alias.get("concurrencyConfiguration"):
        alias_params["concurrencyConfiguration"] = alias["concurrencyConfiguration"]

    print(f"Updating alias {args.flow_alias_id} to version {version}...")
    client.update_flow_alias(**alias_params)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
