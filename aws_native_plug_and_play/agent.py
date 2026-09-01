"""
aws_native_plug_and_play/agent.py — AWS FinOps Cost Anomaly Agent

Agentic loop: detect cost spike → pull CloudTrail changes → Bedrock reasons
about root cause → post Slack alert. Runs on Lambda or locally.

Failure is explicit: if a tool errored or the loop was truncated before the model
finished, lambda_handler raises so the invocation is recorded as an error. A
silent "no anomalies" from a broken query is the one outcome this must never
produce.
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
    logger.info("Cost provider: Athena CUR")
else:
    from tools.aws_cost_explorer import find_spike_services, get_cost_timeseries
    logger.info("Cost provider: AWS Cost Explorer API")

from tools.aws_cloudtrail import find_deploys_near_spike
from tools.slack_notify import post_slack_alert

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
# The happy path is spike -> timeseries -> cloudtrail -> slack -> end_turn = 5
# iterations, and only if the model batches its parallel tool calls. Multi-service
# runs need headroom.
MAX_ITERATIONS = int(os.environ.get("AGENT_MAX_ITERATIONS", "8"))
SPIKE_THRESHOLD_PCT = float(os.environ.get("SPIKE_THRESHOLD_PCT", "25.0"))
BEDROCK_REGION = os.environ.get("AWS_BEDROCK_REGION", "us-east-1")

SYSTEM_PROMPT = """\
You are an AWS FinOps AI agent. You detect cost spikes, correlate them with
CloudTrail infrastructure changes, and post a Slack alert with root cause.

Follow this exact sequence every run:
1. Call find_spike_services with threshold_pct. If it returns an empty list →
   stop and say "No anomalies." Do NOT post to Slack.
2. For each spiked service, call get_cost_timeseries (hours=48) to find the
   spike start hour.
3. Call find_deploys_near_spike for each service using spike_start_iso.
4. Reason about the likely technical cause in 1 sentence.
5. Write 1 actionable fix under 25 words with an estimated dollar saving.
6. Call post_slack_alert with anomalies, causes, suggestions, and run_meta.
   Pass the anomaly objects through unchanged from find_spike_services.

Constraints:
- Never fabricate numbers. Only report what the tool results contain.
- Anomalies are measured on the last COMPLETE day (the `as_of` field), not today.
  Describe them that way — never say "today".
- If a tool returns an error, STOP. Say what failed. Do not retry more than once,
  do not work around it, and do not post a Slack alert based on partial data. A
  missing answer is fine; a wrong answer is not.
- If no deploy is found, say "No deploy found in ±12h window."
- Keep text concise — this goes to busy engineers.
"""

# ---------------------------------------------------------------------------
# Bedrock tool definitions
# ---------------------------------------------------------------------------
BEDROCK_TOOL_DEFINITIONS: list[dict] = [
    {
        "toolSpec": {
            "name": "find_spike_services",
            "description": (
                "Compare the last complete day of AWS spend against the 7-day "
                "baseline. Returns services exceeding threshold_pct."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "threshold_pct": {
                            "type": "number",
                            "description": "Percentage above baseline to flag.",
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
                        "anomalies": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": (
                                "The anomaly objects from find_spike_services, "
                                "passed through unchanged."
                            ),
                        },
                        "causes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "One plain-text root-cause sentence per anomaly, in "
                                "the same order as anomalies. Strings, not objects."
                            ),
                        },
                        "suggestions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "One plain-text fix per anomaly, under 25 words, "
                                "including an estimated dollar saving. Same order as "
                                "anomalies. Strings, not objects."
                            ),
                        },
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
    "get_cost_timeseries": lambda args: get_cost_timeseries(
        args["service"], args.get("hours", 48)
    ),
    "find_deploys_near_spike": lambda args: find_deploys_near_spike(
        args["service"], args["spike_start_iso"], args.get("window_hours", 12)
    ),
    "post_slack_alert": lambda args: post_slack_alert(
        args["anomalies"], args["causes"], args["suggestions"], args["run_meta"]
    ),
}


class AgentRunError(RuntimeError):
    """Raised when the run did not complete cleanly."""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
class NativeAWSFinOpsAgent:
    """Bedrock Converse loop that orchestrates cost anomaly detection."""

    def __init__(self):
        self.run_id = str(uuid.uuid4())
        self.start_time = time.time()
        self.total_tokens = 0
        self.tool_errors: list[str] = []
        self.slack_posted = False
        self.completed = False
        self.bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

    def _dispatch_tool(self, tool_name: str, tool_input: dict) -> dict:
        """Call a tool and wrap the result for the Bedrock Converse API."""
        if tool_name not in TOOL_DISPATCH:
            raise KeyError(f"Unknown tool: {tool_name}")

        if tool_name == "post_slack_alert":
            # Run telemetry is measured, not reported by the model — otherwise the
            # alert carries whatever duration the model happened to guess.
            tool_input = {
                **tool_input,
                "run_meta": {
                    **tool_input.get("run_meta", {}),
                    "run_id": self.run_id,
                    "duration_seconds": round(time.time() - self.start_time, 2),
                },
            }

        result = TOOL_DISPATCH[tool_name](tool_input)
        # Converse requires toolResult.json to be a dict, not a bare list.
        return result if isinstance(result, dict) else {"result": result}

    def run(self) -> dict[str, Any]:
        """Execute the agent loop."""
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "text": (
                            f"Run cost anomaly check. run_id={self.run_id} "
                            f"threshold_pct={SPIKE_THRESHOLD_PCT}"
                        )
                    }
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
                self.completed = True
                break

            if stop_reason != "tool_use":
                self.tool_errors.append(f"Unexpected stop reason: {stop_reason}")
                break

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
                    if name == "post_slack_alert":
                        self.slack_posted = True
                    tool_results.append({
                        "toolResult": {
                            "toolUseId": tid,
                            "content": [{"json": json_result}],
                        }
                    })
                except Exception as e:
                    detail = f"{name}: {type(e).__name__}: {e}"
                    logger.error("Tool error → %s", detail)
                    self.tool_errors.append(detail)
                    tool_results.append({
                        "toolResult": {
                            "toolUseId": tid,
                            "content": [{"text": f"Error: {e}"}],
                            "status": "error",
                        }
                    })

            messages.append({"role": "user", "content": tool_results})

        if not self.completed:
            self.tool_errors.append(
                f"Agent loop exhausted {MAX_ITERATIONS} iterations without finishing"
            )

        return {
            "run_id": self.run_id,
            "duration_seconds": round(time.time() - self.start_time, 2),
            "total_tokens": self.total_tokens,
            "completed": self.completed,
            "slack_posted": self.slack_posted,
            "tool_errors": self.tool_errors,
        }


def lambda_handler(event: dict, context: Any = None) -> dict:
    """
    AWS Lambda entry point.

    Raises on failure so the invocation is recorded in the Errors metric and can
    reach a DLQ. Returning 200 with an error body would leave a broken agent
    indistinguishable from a quiet account.
    """
    agent = NativeAWSFinOpsAgent()
    result = agent.run()
    logger.info("Agent completed: %s", result)

    if result["tool_errors"]:
        raise AgentRunError(
            f"Run {result['run_id']} failed: {'; '.join(result['tool_errors'])}"
        )

    return {"statusCode": 200, "body": result}
