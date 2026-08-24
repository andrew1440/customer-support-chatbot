import json
import os
import uuid
from datetime import datetime, timezone
import boto3

table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])

REQUIRED_FIELDS = ("description", "stepsToReproduce", "environment")

def lambda_handler(event, _):
    print("EVENT:", json.dumps(event, indent=2, default=str))

    # Bedrock Flow Lambda nodes wrap the actual data inside event["node"]["inputs"][0]["value"].
    # Fall back to the raw event itself if that structure isn't present (e.g. direct console tests).
    data = event
    if isinstance(event, dict) and "node" in event and event["node"].get("inputs"):
        data = event["node"]["inputs"][0].get("value", {}) or {}

    description = (data.get("description") or "").strip()
    steps = (data.get("stepsToReproduce") or "").strip()
    environment = (data.get("environment") or "").strip()

    missing = [f for f in REQUIRED_FIELDS if not (data.get(f) or "").strip()]
    if missing:
        return {"error": "missing_fields", "fields": missing}

    ticket_id = str(uuid.uuid4())
    item = {
        "ticketId": ticket_id,
        "description": description,
        "stepsToReproduce": steps,
        "environment": environment,
        "status": "OPEN",
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }

    table.put_item(Item=item)

    return {"ticketId": ticket_id, "status": "OPEN"}