"""
tools/aws_cost_explorer.py — Native AWS Cost Explorer query functions.

Queries AWS Cost Explorer API directly via boto3 (ce:GetCostAndUsage).
No external database or setup required!
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3

logger = logging.getLogger(__name__)


def _get_ce_client():
    """Return a boto3 Cost Explorer client."""
    return boto3.client("ce", region_name="us-east-1")


def get_7day_baseline(service: str | None = None) -> dict[str, float]:
    """
    Calculate 7-day average daily spend per AWS service using AWS Cost Explorer API.
    """
    ce = _get_ce_client()
    today = datetime.now(timezone.utc).date()
    seven_days_ago = today - timedelta(days=7)

    time_period = {
        "Start": seven_days_ago.strftime("%Y-%m-%d"),
        "End": today.strftime("%Y-%m-%d"),
    }

    try:
        response = ce.get_cost_and_usage(
            TimePeriod=time_period,
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )

        service_totals: dict[str, float] = {}
        for result_by_time in response.get("ResultsByTime", []):
            for group in result_by_time.get("Groups", []):
                svc_name = group["Keys"][0]
                cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
                service_totals[svc_name] = service_totals.get(svc_name, 0.0) + cost

        # Calculate daily average over 7 days
        baselines = {
            svc: round(total / 7.0, 2)
            for svc, total in service_totals.items()
            if total > 0
        }

        if service:
            return {service: baselines.get(service, 0.0)}
        return baselines

    except Exception as e:
        logger.error("Failed to query AWS Cost Explorer baseline: %s", e)
        return {}


def get_todays_cost(service: str | None = None) -> dict[str, float]:
    """
    Get today's AWS spend since midnight UTC using AWS Cost Explorer API.
    """
    ce = _get_ce_client()
    today = datetime.now(timezone.utc).date()
    tomorrow = today + timedelta(days=1)

    time_period = {
        "Start": today.strftime("%Y-%m-%d"),
        "End": tomorrow.strftime("%Y-%m-%d"),
    }

    try:
        response = ce.get_cost_and_usage(
            TimePeriod=time_period,
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )

        costs: dict[str, float] = {}
        for result_by_time in response.get("ResultsByTime", []):
            for group in result_by_time.get("Groups", []):
                svc_name = group["Keys"][0]
                cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
                costs[svc_name] = round(cost, 2)

        if service:
            return {service: costs.get(service, 0.0)}
        return costs

    except Exception as e:
        logger.error("Failed to query AWS Cost Explorer today's cost: %s", e)
        return {}


def find_spike_services(threshold_pct: float) -> list[dict[str, Any]]:
    """
    Compare today's AWS spend vs 7-day baseline directly via Cost Explorer.
    Flag services exceeding baseline by >= threshold_pct.
    """
    baselines = get_7day_baseline()
    todays = get_todays_cost()

    anomalies = []
    for service, today_cost in todays.items():
        baseline = baselines.get(service, 0.0)
        if baseline <= 1.0:  # Ignore trivial spend under $1/day
            continue

        pct_change = ((today_cost - baseline) / baseline) * 100
        if pct_change >= threshold_pct:
            anomalies.append({
                "service": service,
                "team": "AWS Account",
                "today_usd": round(today_cost, 2),
                "baseline_usd": round(baseline, 2),
                "delta_usd": round(today_cost - baseline, 2),
                "pct_change": round(pct_change, 1),
            })

    anomalies.sort(key=lambda x: x["delta_usd"], reverse=True)
    logger.info("Cost Explorer flagged %d spike(s) above %.1f%%", len(anomalies), threshold_pct)
    return anomalies


def get_cost_timeseries(service: str, hours: int = 48) -> list[dict[str, Any]]:
    """
    Get hourly cost data for a specific AWS service over the past N hours using Cost Explorer.
    """
    ce = _get_ce_client()
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(hours=hours)

    try:
        response = ce.get_cost_and_usage(
            TimePeriod={
                "Start": start_time.strftime("%Y-%m-%dT%H:00:00Z"),
                "End": now.strftime("%Y-%m-%dT%H:00:00Z"),
            },
            Granularity="HOURLY",
            Filter={
                "Dimensions": {
                    "Key": "SERVICE",
                    "Values": [service],
                }
            },
            Metrics=["UnblendedCost"],
        )

        timeseries = []
        for result in response.get("ResultsByTime", []):
            cost = float(result["Total"]["UnblendedCost"]["Amount"])
            timeseries.append({
                "timestamp": result["TimePeriod"]["Start"],
                "cost_usd": round(cost, 2),
            })

        return timeseries

    except Exception as e:
        logger.error("Failed to query hourly Cost Explorer timeseries for %s: %s", service, e)
        return []
