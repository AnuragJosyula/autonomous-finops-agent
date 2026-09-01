"""
tools/slack_notify.py — Slack Block Kit alert builder for the cost anomaly agent.

Builds and posts one structured message per agent run covering all anomalies,
sorted by dollar delta descending.

Delivery failures raise SlackDeliveryError. An alert that was never seen is a
failed run, not a successful one — the caller needs to know.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

SLACK_TIMEOUT_SECONDS = int(os.environ.get("SLACK_TIMEOUT_SECONDS", "10"))


class SlackDeliveryError(RuntimeError):
    """Raised when the alert could not be delivered to Slack."""


def _webhook_url() -> str:
    """
    Resolve the webhook at call time, not import time, so tests and Lambda
    environment updates are picked up without a module reload.
    """
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        raise SlackDeliveryError(
            "SLACK_WEBHOOK_URL is not set — cannot deliver the cost anomaly alert."
        )
    if not url.startswith("https://hooks.slack.com/"):
        raise SlackDeliveryError(
            f"SLACK_WEBHOOK_URL does not look like a Slack webhook: {url[:40]}..."
        )
    return url


def _as_text(value: Any) -> str:
    """
    Coerce a cause/suggestion into display text.

    The model is asked for strings, but a tool schema cannot hard-guarantee it.
    Rendering str(dict) into a Slack card looks broken, so pull the most likely
    prose field out of an object instead.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("summary", "action", "text", "cause", "suggestion", "description"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        # Fall back to the longest string field rather than dumping the dict.
        strings = [v.strip() for v in value.values() if isinstance(v, str) and v.strip()]
        if strings:
            return max(strings, key=len)
    if isinstance(value, (list, tuple)):
        parts = [_as_text(v) for v in value]
        return " ".join(p for p in parts if p)
    return str(value)


def _build_anomaly_section(
    anomaly: dict[str, Any],
    cause: str,
    suggestion: str,
) -> list[dict]:
    """Build Block Kit blocks for a single anomaly."""
    svc = anomaly.get("service", "unknown service")
    team = anomaly.get("team", "unknown")
    # current_usd is the last complete day, not "today" — CUR and Cost Explorer
    # are both partial for the day in progress.
    current = anomaly.get("current_usd", anomaly.get("today_usd", 0.0))
    baseline = anomaly.get("baseline_usd", 0.0)
    delta = anomaly.get("delta_usd", 0.0)
    pct = anomaly.get("pct_change", 0.0)
    as_of = anomaly.get("as_of")
    day_label = f" ({as_of})" if as_of else ""

    return [
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{svc}*  ·  `{team}`\n"
                    f"Last full day{day_label}: *${current:,.2f}*  (+{pct}% vs 7-day avg)\n"
                    f"Baseline: ${baseline:,.2f}/day  ·  Delta: *+${delta:,.2f}*"
                ),
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*🔍 Root Cause:*\n{cause}"},
                {"type": "mrkdwn", "text": f"*💡 Suggested Fix:*\n{suggestion}"},
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
        anomalies: Anomaly dicts sorted by delta_usd desc.
        causes: Root-cause strings, one per anomaly.
        suggestions: Fix suggestions, one per anomaly.
        run_meta: Dict with run_id and duration_seconds.

    Returns:
        {"delivered": True, "anomaly_count": N} on success.

    Raises:
        SlackDeliveryError: if the webhook is unset, malformed, or the post failed.
    """
    if not anomalies:
        raise SlackDeliveryError(
            "Refusing to post an alert with no anomalies — if nothing spiked, "
            "the run should end without posting."
        )

    url = _webhook_url()

    try:
        duration = float(run_meta.get("duration_seconds", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0

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
                        f"{duration:.1f}s"
                    ),
                }
            ],
        },
    ]

    for i, anomaly in enumerate(anomalies):
        cause = _as_text(causes[i]) if i < len(causes) else "Not determined"
        suggestion = _as_text(suggestions[i]) if i < len(suggestions) else "No suggestion"
        blocks.extend(_build_anomaly_section(anomaly, cause, suggestion))

    # Demo runs use synthetic cost data. Say so in the message rather than
    # leaving a fabricated alert indistinguishable from a real detection.
    if run_meta.get("demo"):
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "⚠️ *Demo run* — synthetic cost data, not a live AWS detection.",
                }
            ],
        })

    payload = json.dumps({"blocks": blocks}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=SLACK_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise SlackDeliveryError(f"Slack returned HTTP {response.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        raise SlackDeliveryError(f"Slack returned HTTP {e.code}: {body}") from e
    except SlackDeliveryError:
        raise
    except Exception as e:
        raise SlackDeliveryError(f"Could not reach Slack: {e}") from e

    logger.info("Slack alert delivered with %d anomaly(ies)", len(anomalies))
    return {"delivered": True, "anomaly_count": len(anomalies)}
