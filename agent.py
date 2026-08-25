"""
agent.py — Cloud Cost Anomaly Agent
Entry point for AWS Lambda. Runs a Bedrock converse loop that detects
AWS cost spikes, correlates them with deploy events, and posts a root-cause
Slack alert.
"""

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3

from tools.elastic_search import find_spike_services, get_cost_timeseries, find_deploys_near_spike
from tools.slack_notify import post_slack_alert
from tools.audit_writer import write_audit

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Configuration — all values come from Lambda environment variables
# ---------------------------------------------------------------------------
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
MAX_ITERATIONS = int(os.environ.get("AGENT_MAX_ITERATIONS", "20"))
SPIKE_THRESHOLD_PCT = float(os.environ.get("SPIKE_THRESHOLD_PCT", "25.0"))
BEDROCK_REGION = os.environ.get("AWS_BEDROCK_REGION", "us-east-1")

# ---------------------------------------------------------------------------
# System prompt — defines Claude's reasoning sequence for every run
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a FinOps AI agent. Every morning you detect AWS cost anomalies,
find their root cause by correlating with recent deployments, and post one Slack alert.

Follow this exact 7-step sequence every run:

Step 1: Call find_spike_services with the provided threshold_pct.
        If the result list is empty, call write_audit with anomalies_found=0,
        slack_delivered=false, then stop. Do NOT post to Slack when there are no anomalies.

Step 2: For each spiked service, call get_cost_timeseries with hours=48.
        Identify the exact hour the spike started (first hour where cost exceeded
        1.5x the prior 6-hour rolling average). Record this as spike_start_iso.

Step 3: Call find_deploys_near_spike for each spiked service using the
        spike_start_iso from step 2 and window_hours=12.

Step 4: Reason about the most likely cause. One sentence, under 30 words.
        Cite the specific technical reason (HPA misconfiguration, traffic spike,
        data transfer, orphaned resource). Do not write vague summaries.
        Example: "HPA scaled checkout pods 3→12 replicas 6h after deploy v2.3.1
        at 14:00 UTC — CPU utilisation held at 18%, minReplicas set too high."

Step 5: Write one actionable fix, under 25 words, with an estimated dollar saving.
        Example: "Reduce minReplicas to 3 in checkout-service/k8s/hpa.yaml.
        Estimated saving if fixed today: ~$220."

Step 6: Call post_slack_alert with all anomalies sorted by delta_usd descending,
        the causes list, the suggestions list, and run_meta containing the run_id
        and duration_seconds so far.

Step 7: Call write_audit with the complete run result. Always call this,
        even if Slack delivery failed.

Constraints:
- Never fabricate numbers. Only report what the tool results contain.
- If a deploy lookup returns no results, say "No deploy found in ±12h window."
- Keep all text concise — this message goes to engineers who are busy.
"""

# ---------------------------------------------------------------------------
# Tool definitions — registered with Bedrock so Claude can call them
# ---------------------------------------------------------------------------
BEDROCK_TOOL_DEFINITIONS: list[dict] = [
    {
        "toolSpec": {
            "name": "find_spike_services",
            "description": (
                "Compare today's AWS spend against the 7-day rolling average per service. "
                "Returns services where today's cost exceeds the threshold percentage above baseline. "
                "Returns an empty list if no anomalies are found."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "threshold_pct": {
                            "type": "number",
                            "description": (
                                "Percentage above 7-day baseline that counts as a spike. "
                                "E.g. 25.0 flags anything up 25% or more."
                            ),
                        }
                    },
                    "required": ["threshold_pct"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_cost_timeseries",
            "description": (
                "Get hourly cost data for a specific AWS service over the past N hours. "
                "Use to pinpoint exactly when a cost spike started."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "service": {
                            "type": "string",
                            "description": "AWS service name, e.g. 'Amazon EC2'.",
                        },
                        "hours": {
                            "type": "integer",
                            "description": "Number of past hours to fetch. Default 48.",
                        },
                    },
                    "required": ["service", "hours"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "find_deploys_near_spike",
            "description": (
                "Find deployments that happened within a time window around a cost spike. "
                "Returns deploy metadata (service, version, team, deployer, commit SHA)."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "service": {
                            "type": "string",
                            "description": "AWS service name to match against deploy events.",
                        },
                        "spike_start_iso": {
                            "type": "string",
                            "description": "ISO 8601 timestamp of when the spike started.",
                        },
                        "window_hours": {
                            "type": "integer",
                            "description": "Hours before/after spike_start to search. Default 12.",
                        },
                    },
                    "required": ["service", "spike_start_iso", "window_hours"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "post_slack_alert",
            "description": (
                "Post a Block Kit message to the #finops Slack channel with all anomalies, "
                "root causes, and suggested fixes. Only call when anomalies are found."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "anomalies": {
                            "type": "array",
                            "description": "List of anomaly objects sorted by delta_usd descending.",
                        },
                        "causes": {
                            "type": "array",
                            "description": "List of root-cause strings, one per anomaly.",
                        },
                        "suggestions": {
                            "type": "array",
                            "description": "List of fix suggestions, one per anomaly.",
                        },
                        "run_meta": {
                            "type": "object",
                            "description": "Run metadata: run_id, duration_seconds.",
                        },
                    },
                    "required": ["anomalies", "causes", "suggestions", "run_meta"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "write_audit",
            "description": (
                "Write an audit record to Elasticsearch. Must be called at the end of every run, "
                "even if no anomalies were found or Slack failed."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string", "description": "UUID of this run."},
                        "anomalies_found": {"type": "integer", "description": "Count of anomalies detected."},
                        "slack_delivered": {"type": "boolean", "description": "Whether Slack message was sent."},
                        "duration_seconds": {"type": "number", "description": "Wall-clock seconds for the run."},
                        "token_count": {"type": "integer", "description": "Total Bedrock tokens used."},
                        "error": {"type": "string", "description": "Error message if any, else null."},
                    },
                    "required": ["run_id", "anomalies_found", "slack_delivered", "duration_seconds", "token_count"],
                }
            },
        }
    },
]

# ---------------------------------------------------------------------------
# Tool dispatcher — maps tool names to Python functions
# ---------------------------------------------------------------------------
TOOL_DISPATCH: dict[str, callable] = {
    "find_spike_services": lambda args: find_spike_services(args["threshold_pct"]),
    "get_cost_timeseries": lambda args: get_cost_timeseries(args["service"], args["hours"]),
    "find_deploys_near_spike": lambda args: find_deploys_near_spike(
        args["service"], args["spike_start_iso"], args["window_hours"]
    ),
    "post_slack_alert": lambda args: post_slack_alert(
        args["anomalies"], args["causes"], args["suggestions"], args["run_meta"]
    ),
    "write_audit": lambda args: write_audit(
        run_id=args["run_id"],
        anomalies_found=args["anomalies_found"],
        slack_delivered=args["slack_delivered"],
        duration_seconds=args["duration_seconds"],
        token_count=args["token_count"],
        error=args.get("error"),
    ),
}


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------
class CloudCostAnomalyAgent:
    """Thin orchestrator: creates a Bedrock converse loop and dispatches tool calls."""

    def __init__(self):
        self.run_id = str(uuid.uuid4())
        self.start_time = time.time()
        self.total_tokens = 0
        self.bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

    def run(self) -> dict[str, Any]:
        """Execute the agent's 7-step reasoning sequence via Bedrock Converse API."""
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "text": (
                            f"Run the cost anomaly check now.\n"
                            f"run_id: {self.run_id}\n"
                            f"threshold_pct: {SPIKE_THRESHOLD_PCT}\n"
                            f"timestamp: {datetime.now(timezone.utc).isoformat()}"
                        )
                    }
                ],
            }
        ]

        for iteration in range(MAX_ITERATIONS):
            logger.info("Converse iteration %d/%d", iteration + 1, MAX_ITERATIONS)

            response = self.bedrock.converse(
                modelId=MODEL_ID,
                system=[{"text": SYSTEM_PROMPT}],
                messages=messages,
                toolConfig={"tools": BEDROCK_TOOL_DEFINITIONS},
            )

            # Track token usage
            usage = response.get("usage", {})
            self.total_tokens += usage.get("inputTokens", 0) + usage.get("outputTokens", 0)

            output_message = response["output"]["message"]
            messages.append(output_message)

            stop_reason = response["stopReason"]

            if stop_reason == "end_turn":
                logger.info("Agent finished (end_turn) after %d iterations", iteration + 1)
                break

            if stop_reason == "tool_use":
                tool_results = []
                for block in output_message["content"]:
                    if "toolUse" in block:
                        tool_use = block["toolUse"]
                        tool_name = tool_use["name"]
                        tool_input = tool_use["input"]
                        tool_use_id = tool_use["toolUseId"]

                        logger.info("Calling tool: %s", tool_name)
                        try:
                            result = TOOL_DISPATCH[tool_name](tool_input)
                            tool_results.append({
                                "toolResult": {
                                    "toolUseId": tool_use_id,
                                    "content": [{"json": result}],
                                }
                            })
                        except Exception as e:
                            logger.error("Tool %s failed: %s", tool_name, e)
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


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------
def lambda_handler(event: dict, context: Any = None) -> dict:
    """AWS Lambda handler — creates the agent and runs it."""
    logger.info("Lambda invoked with event: %s", event)
    agent = CloudCostAnomalyAgent()
    result = agent.run()
    logger.info("Agent completed: %s", result)
    return {
        "statusCode": 200,
        "body": result,
    }
