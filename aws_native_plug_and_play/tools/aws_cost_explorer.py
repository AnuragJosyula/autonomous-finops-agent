"""
tools/aws_cost_explorer.py — AWS Cost Explorer query functions.

Queries Cost Explorer directly via boto3 (ce:GetCostAndUsage). Same contract as
the Athena CUR provider: compare the last complete day against the 7 days before
it, and raise CostQueryError rather than returning empty results on failure.
"""

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import boto3

logger = logging.getLogger(__name__)

# Cost Explorer is a global service — boto3 resolves it to us-east-1 regardless of
# the region passed, so this is here only for endpoint overrides in testing.
CE_REGION = os.environ.get("FINOPS_AWS_REGION") or os.environ.get("AWS_REGION", "us-east-1")

BASELINE_DAYS = 7
MIN_BASELINE_USD = float(os.environ.get("MIN_BASELINE_USD", "1.0"))


class CostQueryError(RuntimeError):
    """Raised when cost data could not be retrieved. Never means 'no spend'."""


def _get_ce_client():
    return boto3.client("ce", region_name=CE_REGION)


def get_daily_costs(days: int = BASELINE_DAYS + 1) -> dict[str, dict[date, float]]:
    """
    Return {service: {usage_date: cost_usd}} for the last `days` complete days.

    The window ends at today (exclusive), so only days that have fully elapsed in
    UTC are included.
    """
    ce = _get_ce_client()
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)

    costs: dict[str, dict[date, float]] = {}
    next_token = None
    try:
        while True:
            kwargs: dict[str, Any] = {
                "TimePeriod": {"Start": start.isoformat(), "End": today.isoformat()},
                "Granularity": "DAILY",
                "Metrics": ["UnblendedCost"],
                "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
            }
            if next_token:
                kwargs["NextPageToken"] = next_token

            response = ce.get_cost_and_usage(**kwargs)

            for bucket in response.get("ResultsByTime", []):
                try:
                    day = date.fromisoformat(bucket["TimePeriod"]["Start"])
                except (KeyError, ValueError):
                    continue
                for group in bucket.get("Groups", []):
                    service = group["Keys"][0]
                    amount = group["Metrics"]["UnblendedCost"]["Amount"]
                    try:
                        cost = float(amount)
                    except (TypeError, ValueError):
                        continue
                    costs.setdefault(service, {})[day] = cost

            next_token = response.get("NextPageToken")
            if not next_token:
                break
    except Exception as e:
        raise CostQueryError(f"Cost Explorer daily cost query failed: {e}") from e

    return costs


def find_spike_services(threshold_pct: float = 25.0) -> list[dict[str, Any]]:
    """
    Compare the most recent complete day against the 7 days before it.

    Deliberately not "today so far": the agent runs at 08:00 UTC, so today would
    be ~8 hours of spend measured against a 24-hour baseline and would essentially
    never cross the threshold.
    """
    daily = get_daily_costs()
    if not daily:
        raise CostQueryError(
            "Cost Explorer returned no cost data. Check that Cost Explorer is "
            "enabled for this account and that ce:GetCostAndUsage is permitted."
        )

    all_days = sorted({d for series in daily.values() for d in series})
    if not all_days:
        raise CostQueryError("Cost Explorer returned no dated cost buckets.")

    target_day = max(all_days)
    window_start = target_day - timedelta(days=BASELINE_DAYS)
    baseline_days = [d for d in all_days if window_start <= d < target_day]
    if not baseline_days:
        raise CostQueryError(
            f"Only one day of cost data ({target_day}) — need at least two to "
            "build a baseline."
        )

    divisor = len(baseline_days)
    anomalies = []
    for service, series in daily.items():
        current = series.get(target_day, 0.0)
        baseline = sum(series.get(d, 0.0) for d in baseline_days) / divisor
        if baseline <= MIN_BASELINE_USD:
            continue

        pct_change = ((current - baseline) / baseline) * 100
        if pct_change >= threshold_pct:
            anomalies.append({
                "service": service,
                "team": "AWS Account",
                "as_of": target_day.isoformat(),
                "current_usd": round(current, 2),
                "baseline_usd": round(baseline, 2),
                "delta_usd": round(current - baseline, 2),
                "pct_change": round(pct_change, 1),
            })

    anomalies.sort(key=lambda a: a["delta_usd"], reverse=True)
    logger.info(
        "Cost Explorer: %d spike(s) above %.1f%% for %s vs %d-day baseline",
        len(anomalies), threshold_pct, target_day, divisor,
    )
    return anomalies


def get_cost_timeseries(service: str, hours: int = 48) -> list[dict[str, Any]]:
    """Hourly cost for one service over the past N hours, to pinpoint the spike start."""
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
            Filter={"Dimensions": {"Key": "SERVICE", "Values": [service]}},
            Metrics=["UnblendedCost"],
        )
    except Exception as e:
        raise CostQueryError(
            f"Cost Explorer hourly timeseries for {service!r} failed: {e}. "
            "Hourly granularity requires Cost Explorer hourly data to be enabled."
        ) from e

    timeseries = []
    for bucket in response.get("ResultsByTime", []):
        try:
            cost = float(bucket["Total"]["UnblendedCost"]["Amount"])
        except (KeyError, TypeError, ValueError):
            continue
        timeseries.append({
            "timestamp": bucket["TimePeriod"]["Start"],
            "cost_usd": round(cost, 2),
        })
    return timeseries
