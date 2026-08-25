"""
tools/aws_cloudtrail.py — Native AWS CloudTrail event lookup functions.

Queries AWS CloudTrail API directly via boto3 (cloudtrail:LookupEvents).
Searches for recent infrastructure modifications, scaling, and deployment calls.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3

logger = logging.getLogger(__name__)


def _get_cloudtrail_client():
    """Return a boto3 CloudTrail client."""
    return boto3.client("cloudtrail", region_name="us-east-1")


def find_deploys_near_spike(
    service: str, spike_start_iso: str, window_hours: int = 12
) -> list[dict[str, Any]]:
    """
    Find infrastructure modifications and deployments near spike_start_iso
    using AWS CloudTrail event history.
    """
    ct = _get_cloudtrail_client()
    try:
        spike_time = datetime.fromisoformat(spike_start_iso.replace("Z", "+00:00"))
    except Exception:
        spike_time = datetime.now(timezone.utc)

    start_time = spike_time - timedelta(hours=window_hours)
    end_time = spike_time + timedelta(hours=window_hours)

    try:
        response = ct.lookup_events(
            StartTime=start_time,
            EndTime=end_time,
            MaxResults=20,
        )

        deploys = []
        for event in response.get("Events", []):
            event_name = event.get("EventName", "")
            # Filter for impactful infrastructure / deployment event names
            if any(keyword in event_name for keyword in [
                "RunInstances", "CreateCluster", "UpdateService",
                "ModifyDBInstance", "CreateDeployment", "PutScalingPolicy",
                "ModifyAutoScalingGroup", "UpdateStack"
            ]):
                event_time = event.get("EventTime", datetime.now(timezone.utc))
                hours_before = (spike_time - event_time).total_seconds() / 3600

                deploys.append({
                    "service": service,
                    "event_name": event_name,
                    "username": event.get("Username", "AWS-User"),
                    "timestamp": event_time.isoformat(),
                    "hours_before_spike": round(hours_before, 1),
                    "resource_name": event.get("Resources", [{}])[0].get("ResourceName", "N/A"),
                })

        logger.info("CloudTrail found %d event(s) near spike", len(deploys))
        return deploys

    except Exception as e:
        logger.error("CloudTrail lookup failed: %s", e)
        return []
