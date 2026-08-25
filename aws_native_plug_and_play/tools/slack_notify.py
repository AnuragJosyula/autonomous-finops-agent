"""
tools/slack_notify.py — Slack Block Kit alert builder for the cost anomaly agent.

Builds and posts one structured message per agent run covering all anomalies,
sorted by dollar delta descending.
"""

import json
import logging
import os
from typing import Any

import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

# Read directly from env var (no Secrets Manager)
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")


def _build_anomaly_section(
    anomaly: dict[str, Any],
    cause: str,
    suggestion: str,
) -> list[dict]:
    """Build Block Kit blocks for a single anomaly."""
    svc = anomaly["service"]
    team = anomaly.get("team", "unknown")
    today = anomaly["today_usd"]
    baseline = anomaly["baseline_usd"]
    delta = anomaly["delta_usd"]
    pct = anomaly["pct_change"]

    return [
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{svc}*  ·  `{team}`\n"
                    f"Today: *${today:,.2f}*  (+{pct}% vs 7-day avg)\n"
                    f"Baseline: ${baseline:,.2f}/day  ·  Delta: *+${delta:,.2f}*"
                ),
            },
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*🔍 Root Cause:*\n{cause}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*💡 Suggested Fix:*\n{suggestion}",
                },
            ],
        },
    ]


def post_slack_alert(
    anomalies: list[dict],
    causes: list[str],
    suggestions: list[str],
    run_meta: dict[str, Any],
) -> dict[str, Any]:
    """
    Post a Block Kit message to Slack with all anomalies.

    Args:
        anomalies: List of anomaly dicts sorted by delta_usd desc.
        causes: Root-cause strings, one per anomaly.
        suggestions: Fix suggestions, one per anomaly.
        run_meta: Dict with run_id and duration_seconds.

    Returns:
        Dict with delivered=True/False and any error message.
    """
    # Build the Block Kit message
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🔴 AWS Cost Anomaly Detected",
                "emoji": True,
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Run `{run_meta.get('run_id', 'unknown')}`  ·  "
                        f"{len(anomalies)} anomaly(ies) found  ·  "
                        f"{run_meta.get('duration_seconds', 0):.1f}s"
                    ),
                }
            ],
        },
    ]

    # Add a section for each anomaly
    for i, anomaly in enumerate(anomalies):
        cause = causes[i] if i < len(causes) else "Unknown"
        suggestion = suggestions[i] if i < len(suggestions) else "No suggestion"
        blocks.extend(_build_anomaly_section(anomaly, cause, suggestion))

    # Post to Slack
    payload = json.dumps({"blocks": blocks}).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            delivered = resp.status == 200
            logger.info("Slack message delivered: %s", delivered)
            return {"delivered": delivered, "error": None}
    except urllib.error.HTTPError as e:
        error_msg = f"Slack HTTP {e.code}: {e.read().decode()[:200]}"
        logger.error(error_msg)
        return {"delivered": False, "error": error_msg}
    except Exception as e:
        error_msg = f"Slack error: {e}"
        logger.error(error_msg)
        return {"delivered": False, "error": error_msg}
