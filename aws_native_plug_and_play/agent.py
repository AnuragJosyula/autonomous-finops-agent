"""
aws_native_plug_and_play/agent.py — Native AWS FinOps Cost Anomaly Agent

Entry point for AWS Lambda (Plug & Play version).
Queries AWS Cost Explorer & CloudTrail directly via boto3 — zero databases required!
"""

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3

COST_PROVIDER = os.environ.get("COST_PROVIDER", "COST_EXPLORER").upper()

if COST_PROVIDER == "ATHENA_CUR":
    from tools.aws_athena_cur import find_spike_services, get_cost_timeseries
    logger.info("Using Enterprise Athena CUR (FOCUS) cost provider")
else:
    from tools.aws_cost_explorer import find_spike_services, get_cost_timeseries
    logger.info("Using AWS Cost Explorer API cost provider")

from tools.aws_cloudtrail import find_deploys_near_spike
from tools.slack_notify import post_slack_alert

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Configuration
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-3-5-sonnet-20241022-v2:0")
MAX_ITERATIONS = int(os.environ.get("AGENT_MAX_ITERATIONS", "20"))
SPIKE_THRESHOLD_PCT = float(os.environ.get("SPIKE_THRESHOLD_PCT", "25.0"))
BEDROCK_REGION = os.environ.get("AWS_BEDROCK_REGION", "us-east-1")

SYSTEM_PROMPT = """You are an AWS FinOps AI agent. You detect cost spikes in AWS Cost Explorer,
correlate them with recent AWS CloudTrail infrastructure changes, and post a Slack alert.

Follow this exact sequence every run:
Step 1: Call find_spike_services with threshold_pct. If empty, stop. Do NOT post to Slack.
Step 2: For each spiked service, call get_cost_timeseries with hours=48 to identify the spike start hour.
Step 3: Call find_deploys_near_spike for each service using spike_start_iso.
Step 4: Reason about the likely technical cause in 1 sentence.
Step 5: Write 1 actionable fix under 25 words with an estimated dollar saving.
Step 6: Call post_slack_alert with anomalies, causes, suggestions, and run_meta.
"""

BEDROCK_TOOL_DEFINITIONS: list[dict] = [
    {
        "toolSpec": {
            "name": "find_spike_services",
            "description": "Compare today's AWS spend vs 7-day baseline directly using AWS Cost Explorer API.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "threshold_pct": {"type": "number", "description": "Percentage above 7-day baseline."}
                    },
                    "required": ["threshold_pct"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_cost_timeseries",
            "description": "Get hourly cost metrics for a specific AWS service from Cost Explorer.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "service": {"type": "string"},
                        "hours": {"type": "integer"},
                    },
                    "required": ["service", "hours"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "find_deploys_near_spike",
            "description": "Find infrastructure changes in AWS CloudTrail near the cost spike time.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "service": {"type": "string"},
                        "spike_start_iso": {"type": "string"},
                        "window_hours": {"type": "integer"},
                    },
                    "required": ["service", "spike_start_iso", "window_hours"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "post_slack_alert",
            "description": "Post Slack Block Kit alert message.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "anomalies": {"type": "array"},
                        "causes": {"type": "array"},
                        "suggestions": {"type": "array"},
                        "run_meta": {"type": "object"},
                    },
                    "required": ["anomalies", "causes", "suggestions", "run_meta"],
                }
            },
        }
    },
]

TOOL_DISPATCH: dict[str, callable] = {
    "find_spike_services": lambda args: find_spike_services(args["threshold_pct"]),
    "get_cost_timeseries": lambda args: get_cost_timeseries(args["service"], args["hours"]),
    "find_deploys_near_spike": lambda args: find_deploys_near_spike(
        args["service"], args["spike_start_iso"], args.get("window_hours", 12)
    ),
    "post_slack_alert": lambda args: post_slack_alert(
        args["anomalies"], args["causes"], args["suggestions"], args["run_meta"]
    ),
}


class NativeAWSFinOpsAgent:
    """100% Native AWS FinOps agent — no external database required."""

    def __init__(self):
        self.run_id = str(uuid.uuid4())
        self.start_time = time.time()
        self.total_tokens = 0
        self.bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

    def run(self) -> dict[str, Any]:
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "text": f"Run native AWS cost anomaly check. run_id: {self.run_id}, threshold_pct: {SPIKE_THRESHOLD_PCT}"
                    }
                ],
            }
        ]

        for iteration in range(MAX_ITERATIONS):
            response = self.bedrock.converse(
                modelId=MODEL_ID,
                system=[{"text": SYSTEM_PROMPT}],
                messages=messages,
                toolConfig={"tools": BEDROCK_TOOL_DEFINITIONS},
            )

            usage = response.get("usage", {})
            self.total_tokens += usage.get("inputTokens", 0) + usage.get("outputTokens", 0)

            output_message = response["output"]["message"]
            messages.append(output_message)

            stop_reason = response["stopReason"]
            if stop_reason == "end_turn":
                break

            if stop_reason == "tool_use":
                tool_results = []
                for block in output_message["content"]:
                    if "toolUse" in block:
                        tool_use = block["toolUse"]
                        tool_name = tool_use["name"]
                        tool_input = tool_use["input"]
                        tool_use_id = tool_use["toolUseId"]

                        try:
                            result = TOOL_DISPATCH[tool_name](tool_input)
                            tool_results.append({
                                "toolResult": {
                                    "toolUseId": tool_use_id,
                                    "content": [{"json": result}],
                                }
                            })
                        except Exception as e:
                            tool_results.append({
                                "toolResult": {
                                    "toolUseId": tool_use_id,
                                    "content": [{"text": f"Error: {e}"}],
                                    "status": "error",
                                }
                            })

                messages.append({"role": "user", "content": tool_results})

        duration = time.time() - self.start_time
        return {
            "run_id": self.run_id,
            "duration_seconds": round(duration, 2),
            "total_tokens": self.total_tokens,
        }


def lambda_handler(event: dict, context: Any = None) -> dict:
    agent = NativeAWSFinOpsAgent()
    result = agent.run()
    return {"statusCode": 200, "body": result}
