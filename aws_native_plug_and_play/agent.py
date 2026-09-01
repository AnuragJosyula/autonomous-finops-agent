"""
aws_native_plug_and_play/agent.py — AWS FinOps Cost Anomaly Agent

Agentic loop: detect cost spike → pull CloudTrail changes → Bedrock reasons
about root cause → post Slack alert. Runs on Lambda or locally.
"""

import logging
import os
import time
import uuid
from typing import Any

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Cost provider selection
# ---------------------------------------------------------------------------
COST_PROVIDER = os.environ.get("COST_PROVIDER", "COST_EXPLORER").upper()

if COST_PROVIDER == "ATHENA_CUR":
    from tools.aws_athena_cur import find_spike_services, get_cost_timeseries
    logger.info("Cost provider: Athena CUR (FOCUS)")
else:
    from tools.aws_cost_explorer import find_spike_services, get_cost_timeseries
    logger.info("Cost provider: AWS Cost Explorer API")

from tools.aws_cloudtrail import find_deploys_near_spike
from tools.slack_notify import post_slack_alert

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
MAX_ITERATIONS = int(os.environ.get("AGENT_MAX_ITERATIONS", "5"))
SPIKE_THRESHOLD_PCT = float(os.environ.get("SPIKE_THRESHOLD_PCT", "25.0"))
BEDROCK_REGION = os.environ.get("AWS_BEDROCK_REGION", "us-east-1")

SYSTEM_PROMPT = """\
You are an AWS FinOps AI agent. You detect cost spikes, correlate them with
CloudTrail infrastructure changes, and post a Slack alert with root cause.

Follow this exact sequence every run:
1. Call find_spike_services with threshold_pct. If empty → stop. Do NOT post to Slack.
2. For each spiked service, call get_cost_timeseries (hours=48) to find the spike start hour.
3. Call find_deploys_near_spike for each service using spike_start_iso.
4. Reason about the likely technical cause in 1 sentence.
5. Write 1 actionable fix under 25 words with an estimated dollar saving.
6. Call post_slack_alert with anomalies, causes, suggestions, and run_meta.

Constraints:
- Never fabricate numbers. Only report what the tool results contain.
- If no deploy found, say "No deploy found in ±12h window."
- Keep text concise — this goes to busy engineers.
"""

# ---------------------------------------------------------------------------
# Bedrock tool definitions
# ---------------------------------------------------------------------------
BEDROCK_TOOL_DEFINITIONS: list[dict] = [
    {
        "toolSpec": {
            "name": "find_spike_services",
            "description": "Compare today's AWS spend vs 7-day baseline. Returns services exceeding threshold.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "threshold_pct": {"type": "number", "description": "Percentage above baseline to flag."}
                    },
                    "required": ["threshold_pct"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_cost_timeseries",
            "description": "Get hourly cost data for an AWS service to pinpoint spike start.",
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
            "description": "Find CloudTrail infrastructure changes near the cost spike time.",
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
            "description": "Post Block Kit alert to Slack with anomalies, causes, and fixes.",
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

# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------
TOOL_DISPATCH = {
    "find_spike_services": lambda args: find_spike_services(args["threshold_pct"]),
    "get_cost_timeseries": lambda args: get_cost_timeseries(args["service"], args["hours"]),
    "find_deploys_near_spike": lambda args: find_deploys_near_spike(
        args["service"], args["spike_start_iso"], args.get("window_hours", 12)
    ),
    "post_slack_alert": lambda args: post_slack_alert(
        args["anomalies"], args["causes"], args["suggestions"], args["run_meta"]
    ),
}


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
class NativeAWSFinOpsAgent:
    """Bedrock Converse loop that orchestrates cost anomaly detection."""

    def __init__(self):
        self.run_id = str(uuid.uuid4())
        self.start_time = time.time()
        self.total_tokens = 0
        self.bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

    def _dispatch_tool(self, tool_name: str, tool_input: dict) -> dict:
        """Call a tool and wrap the result for Bedrock Converse API."""
        result = TOOL_DISPATCH[tool_name](tool_input)
        # Bedrock Converse requires toolResult.json to be a dict, not a list.
        return result if isinstance(result, dict) else {"result": result}

    def run(self) -> dict[str, Any]:
        """Execute the agent loop."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"text": f"Run cost anomaly check. run_id={self.run_id} threshold_pct={SPIKE_THRESHOLD_PCT}"}
                ],
            }
        ]

        for iteration in range(MAX_ITERATIONS):
            logger.info("── Iteration %d/%d ──", iteration + 1, MAX_ITERATIONS)

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

            for block in output_message["content"]:
                if "text" in block:
                    logger.info("Model: %s", block["text"][:500])

            stop_reason = response["stopReason"]
            logger.info("Stop: %s | Tokens: %d", stop_reason, self.total_tokens)

            if stop_reason == "end_turn":
                break

            if stop_reason == "tool_use":
                tool_results = []
                for block in output_message["content"]:
                    if "toolUse" not in block:
                        continue
                    tool_use = block["toolUse"]
                    name = tool_use["name"]
                    args = tool_use["input"]
                    tid = tool_use["toolUseId"]

                    logger.info("Tool: %s(%s)", name, args)
                    try:
                        json_result = self._dispatch_tool(name, args)
                        logger.info("Result: %s", str(json_result)[:500])
                        tool_results.append({
                            "toolResult": {
                                "toolUseId": tid,
                                "content": [{"json": json_result}],
                            }
                        })
                    except Exception as e:
                        logger.error("Tool error: %s → %s", name, e)
                        tool_results.append({
                            "toolResult": {
                                "toolUseId": tid,
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
    """AWS Lambda entry point."""
    agent = NativeAWSFinOpsAgent()
    result = agent.run()
    logger.info("Agent completed: %s", result)
    return {"statusCode": 200, "body": result}
