#!/usr/bin/env python3
"""
scripts/demo_alert.py — End-to-end demo of the alert path with synthetic cost data.

Runs the real agent: the real Bedrock Converse loop, the real system prompt, the
real Slack Block Kit builder. Only the three read-only data tools are replaced
with fixtures, because an account with no spend has no spike to detect.

What this proves: the agent loop, the model's root-cause reasoning, the
remediation wording, and Slack delivery all work.
What it does not prove: the Athena/Cost Explorer queries. Use
scripts/validate_athena.py for those.

Usage:
  export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
  python scripts/demo_alert.py

  # Drop the demo banner from the Slack message (say it's a demo yourself):
  python scripts/demo_alert.py --no-banner
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aws_native_plug_and_play"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger("demo")

# ---------------------------------------------------------------------------
# Fixtures — a plausible HPA misconfiguration after a deploy.
# ---------------------------------------------------------------------------
_YESTERDAY = (datetime.now(timezone.utc) - timedelta(days=1)).date()
_SPIKE_START = datetime.combine(_YESTERDAY, datetime.min.time()).replace(
    hour=17, tzinfo=timezone.utc
)

DEMO_ANOMALIES = [
    {
        "service": "Amazon Elastic Compute Cloud - Compute",
        "team": "AWS Account",
        "as_of": _YESTERDAY.isoformat(),
        "current_usd": 847.20,
        "baseline_usd": 592.10,
        "delta_usd": 255.10,
        "pct_change": 43.1,
    }
]


def _demo_timeseries(service: str, hours: int = 48):
    """Flat overnight, then a step change at 17:00 — the shape of a scaling event."""
    base = _SPIKE_START - timedelta(hours=9)
    flat = [24.6, 24.7, 25.0, 25.0, 25.1, 25.0, 25.0, 24.9, 25.0]
    ramp = [26.1, 28.4, 34.9, 41.2, 43.9, 44.0, 44.1, 43.8]
    series = []
    for i, cost in enumerate(flat + ramp):
        series.append({
            "timestamp": (base + timedelta(hours=i)).isoformat(),
            "cost_usd": cost,
        })
    return series


def _demo_deploys(service: str, spike_start_iso: str, window_hours: int = 12):
    return [
        {
            "service": service,
            "event_name": "ModifyAutoScalingGroup",
            "username": "eks-hpa-controller",
            "timestamp": (_SPIKE_START - timedelta(hours=3)).isoformat(),
            "hours_before_spike": 3.0,
            "resource_name": "eks-checkout-nodegroup",
        },
        {
            "service": service,
            "event_name": "RunInstances",
            "username": "eks-hpa-controller",
            "timestamp": (_SPIKE_START - timedelta(hours=2, minutes=45)).isoformat(),
            "hours_before_spike": 2.8,
            "resource_name": "i-0a91c4f2be77d3e10 (m5.2xlarge x9)",
        },
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-banner",
        action="store_true",
        help="omit the 'demo run' notice from the Slack message",
    )
    args = parser.parse_args()

    if not os.environ.get("SLACK_WEBHOOK_URL", "").strip():
        print(
            "SLACK_WEBHOOK_URL is not set.\n\n"
            "Create an incoming webhook at https://api.slack.com/messaging/webhooks\n"
            "then:  export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...",
            file=sys.stderr,
        )
        return 2

    import agent as agent_module

    # Swap only the read-only data tools. post_slack_alert stays real, and so
    # does every line of the Bedrock loop.
    agent_module.TOOL_DISPATCH["find_spike_services"] = lambda a: DEMO_ANOMALIES
    agent_module.TOOL_DISPATCH["get_cost_timeseries"] = lambda a: _demo_timeseries(
        a["service"], a.get("hours", 48)
    )
    agent_module.TOOL_DISPATCH["find_deploys_near_spike"] = lambda a: _demo_deploys(
        a["service"], a["spike_start_iso"], a.get("window_hours", 12)
    )

    if not args.no_banner:
        real_post = agent_module.post_slack_alert

        def post_with_banner(anomalies, causes, suggestions, run_meta):
            run_meta = {**run_meta, "demo": True}
            return real_post(anomalies, causes, suggestions, run_meta)

        agent_module.TOOL_DISPATCH["post_slack_alert"] = lambda a: post_with_banner(
            a["anomalies"], a["causes"], a["suggestions"], a["run_meta"]
        )

    print("=" * 64)
    print("  Demo alert — synthetic cost data, real agent loop")
    print(f"  Model: {agent_module.MODEL_ID}")
    print(f"  Simulated spike: EC2 +43.1% on {_YESTERDAY}")
    print("=" * 64)
    print()

    result = agent_module.NativeAWSFinOpsAgent().run()

    print()
    print("=" * 64)
    for key in ("run_id", "duration_seconds", "total_tokens", "completed", "slack_posted"):
        print(f"  {key:18} {result[key]}")
    if result["tool_errors"]:
        print(f"  {'tool_errors':18} {result['tool_errors']}")
    print("=" * 64)

    if result["tool_errors"]:
        print("\nRun did not complete cleanly.")
        return 1
    if not result["slack_posted"]:
        print("\nAgent finished but never posted to Slack — check the log above.")
        return 1

    print("\nSlack alert delivered. Check the channel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
