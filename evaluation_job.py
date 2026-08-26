"""
evaluation_job.py

Invokes an Amazon Bedrock Flow against every prompt in flow-tests.json,
writes the results as a JSONL file in the schema expected by Bedrock
Knowledge Base / Model Evaluation ("Bring your own inference responses"),
and uploads the file to S3 so it can be used as input for a Bedrock
Evaluation job in the console.

Prerequisites:
    pip install boto3
    AWS credentials configured (aws configure, or an execution role)
    The flow must already be published with an alias (FLOW_ALIAS_ID)

Fill in the CONFIG block below before running.
"""

import boto3
import json
import os
import time
import uuid

# ---------------------------------------------------------------------------
# CONFIG - fill these in for your environment
# ---------------------------------------------------------------------------
REGION = "us-east-1"                       # AWS region your flow is deployed in
FLOW_ID = "5DCGJQ508A"           # e.g. "ABCD1234EF"
FLOW_ALIAS_ID = "PFESM2MECD"    # e.g. "TSTALIASID" or your published alias
FLOW_INPUT_NODE_NAME = "FlowInputNode"     # name of the Flow input node
FLOW_INPUT_FIELD = "document"      # the input field the flow expects

TEST_FILE = "flow-tests.json"
OUTPUT_FILE = "flow-eval-output.jsonl"

S3_BUCKET = os.getenv("EVAL_DATASET_BUCKET", "udacity-agentic-engineer-c1-eval-219967979764")
S3_PREFIX = "bedrock-flow-evaluations/customer-request-flow/"

# ---------------------------------------------------------------------------

def invoke_flow(client, prompt: str) -> str:
    """Invoke the Bedrock flow for a single prompt and return the text output."""
    response = client.invoke_flow(
        flowIdentifier=FLOW_ID,
        flowAliasIdentifier=FLOW_ALIAS_ID,
        inputs=[
            {
                "content": {"document": prompt},
                "nodeName": FLOW_INPUT_NODE_NAME,
                "nodeOutputName": FLOW_INPUT_FIELD,
            }
        ],
    )

    output_text = ""
    for event in response["responseStream"]:
        if "flowOutputEvent" in event:
            output_text = event["flowOutputEvent"]["content"]["document"]
        elif "flowCompletionEvent" in event:
            completion_reason = event["flowCompletionEvent"]["completionReason"]
            if completion_reason != "SUCCESS":
                output_text = f"[FLOW_ERROR: {completion_reason}]"
    return output_text


def main():
    with open(TEST_FILE, "r", encoding="utf-8") as f:
        tests = json.load(f)["tests"]

    flow_client = boto3.client("bedrock-agent-runtime", region_name=REGION)

    records = []
    for test in tests:
        print(f"Running test: {test['id']} ({test['category']})")
        try:
            model_output = invoke_flow(flow_client, test["prompt"])
        except Exception as e:
            model_output = f"[EXCEPTION: {e}]"
        print(f"  -> {model_output[:120]}")

        # Schema for Bedrock "bring your own inference response" evaluation.
        # See: Bedrock Evaluations custom metrics / BYOI JSONL format.
        record = {
            "conversationTurns": [
                {
                    "prompt": {
                        "content": [{"text": test["prompt"]}]
                    },
                    "referenceResponses": [
                        {
                            "content": [{"text": test.get("reference_response", "")}]
                        }
                    ],
                    "output": {
                        "text": model_output
                    }
                }
            ],
            "metadata": {
                "testId": test["id"],
                "category": test["category"],
            },
        }
        records.append(record)
        time.sleep(0.5)  # small pacing buffer between flow invocations

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\nWrote {len(records)} records to {OUTPUT_FILE}")

    # Upload to S3
    s3_client = boto3.client("s3", region_name=REGION)
    s3_key = f"{S3_PREFIX}{OUTPUT_FILE}"
    s3_client.upload_file(OUTPUT_FILE, S3_BUCKET, s3_key)
    print(f"Uploaded to s3://{S3_BUCKET}/{s3_key}")
    print("\nUse this S3 URI as the evaluation dataset input in Bedrock Evaluations.")


if __name__ == "__main__":
    main()
