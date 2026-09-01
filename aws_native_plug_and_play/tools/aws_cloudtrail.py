"""
tools/aws_cloudtrail.py — CloudTrail event lookup for correlating cost spikes
with infrastructure changes.

Queries CloudTrail directly via boto3 (cloudtrail:LookupEvents), looking for
scaling, deployment, and provisioning calls around the spike window.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3

logger = logging.getLogger(__name__)

# AWS_REGION is reserved in Lambda and cannot be set on a function's environment,
# so FINOPS_AWS_REGION takes precedence. CloudTrail is regional: events are only
# visible in the region they were made in (plus global-service events).
CLOUDTRAIL_REGION = os.environ.get("FINOPS_AWS_REGION") or os.environ.get("AWS_REGION", "us-east-1")

# 50 events/page x 10 pages = 500 events examined per lookup.
_MAX_PAGES = 10
_PAGE_SIZE = 50

_IMPACT_KEYWORDS = (
    "RunInstances",
    "CreateCluster",
    "UpdateService",
    "ModifyDBInstance",
    "CreateDeployment",
    "PutScalingPolicy",
    "ModifyAutoScalingGroup",
    "UpdateStack",
    "CreateFunction",
    "UpdateFunctionConfiguration",
    "StartDBCluster",
)


class CloudTrailLookupError(RuntimeError):
    """Raised when CloudTrail could not be queried. Never means 'no deploys'."""


def _get_cloudtrail_client():
    return boto3.client("cloudtrail", region_name=CLOUDTRAIL_REGION)


def find_deploys_near_spike(
    service: str, spike_start_iso: str, window_hours: int = 12
) -> list[dict[str, Any]]:
    """
    Find infrastructure changes within +/- window_hours of the spike start.

    Returns an empty list when the window genuinely contains no matching events;
    raises CloudTrailLookupError when the lookup itself failed.
    """
    try:
        spike_time = datetime.fromisoformat(spike_start_iso.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise ValueError(
            f"spike_start_iso is not an ISO-8601 timestamp: {spike_start_iso!r}"
        )

    if spike_time.tzinfo is None:
        spike_time = spike_time.replace(tzinfo=timezone.utc)

    start_time = spike_time - timedelta(hours=window_hours)
    end_time = spike_time + timedelta(hours=window_hours)

    ct = _get_cloudtrail_client()
    deploys: list[dict[str, Any]] = []
    next_token = None
    pages_read = 0

    try:
        for _ in range(_MAX_PAGES):
            kwargs: dict[str, Any] = {
                "StartTime": start_time,
                "EndTime": end_time,
                "MaxResults": _PAGE_SIZE,
            }
            if next_token:
                kwargs["NextToken"] = next_token

            response = ct.lookup_events(**kwargs)
            pages_read += 1

            for event in response.get("Events", []):
                event_name = event.get("EventName", "")
                if not any(kw in event_name for kw in _IMPACT_KEYWORDS):
                    continue

                event_time = event.get("EventTime")
                if event_time is None:
                    continue
                if event_time.tzinfo is None:
                    event_time = event_time.replace(tzinfo=timezone.utc)

                resources = event.get("Resources") or [{}]
                deploys.append({
                    "service": service,
                    "event_name": event_name,
                    "username": event.get("Username", "unknown"),
                    "timestamp": event_time.isoformat(),
                    "hours_before_spike": round(
                        (spike_time - event_time).total_seconds() / 3600, 1
                    ),
                    "resource_name": resources[0].get("ResourceName", "N/A"),
                })

            next_token = response.get("NextToken")
            if not next_token:
                break
    except Exception as e:
        raise CloudTrailLookupError(
            f"CloudTrail lookup failed in {CLOUDTRAIL_REGION}: {e}"
        ) from e

    if next_token:
        logger.warning(
            "CloudTrail results truncated at %d pages — some events in the window "
            "were not examined.", pages_read,
        )

    deploys.sort(key=lambda d: abs(d["hours_before_spike"]))
    logger.info(
        "CloudTrail: %d matching event(s) within +/-%dh of %s",
        len(deploys), window_hours, spike_time.isoformat(),
    )
    return deploys
