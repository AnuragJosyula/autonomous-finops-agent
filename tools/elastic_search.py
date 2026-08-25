"""
tools/elastic_search.py — Elasticsearch query functions for the cost anomaly agent.

All functions used by the Bedrock tool-calling loop:
  - get_7day_baseline      : rolling 7-day average spend per service
  - get_todays_cost        : today's spend since midnight UTC
  - find_spike_services    : compare today vs baseline, return anomalies
  - get_cost_timeseries    : hourly cost for spike-start pinpointing
  - find_deploys_near_spike: correlate a spike with deploy events
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from elasticsearch import Elasticsearch

logger = logging.getLogger(__name__)

# Read directly from env vars (no Secrets Manager)
ES_URL = os.environ.get("ES_URL", "")
ES_API_KEY = os.environ.get("ES_API_KEY", "")

BILLING_INDEX = "metrics-aws.billing-*"
DEPLOY_INDEX = "deploy-events-*"


# ---------------------------------------------------------------------------
# Client — lazy-initialised and cached for Lambda container reuse
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _get_es_client() -> Elasticsearch:
    """
    Return an authenticated Elasticsearch client using env var credentials.
    Called once per Lambda execution environment (cached).
    """
    client = Elasticsearch(
        ES_URL,
        api_key=ES_API_KEY,
        request_timeout=30,
        retry_on_timeout=True,
        max_retries=3,
    )
    logger.info("Elasticsearch client initialised for %s", ES_URL)
    return client


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _midnight_utc() -> str:
    """Return today's midnight in ISO 8601 UTC."""
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.isoformat()


def _days_ago_utc(days: int) -> str:
    """Return the ISO 8601 UTC timestamp N days ago."""
    ts = datetime.now(timezone.utc) - timedelta(days=days)
    return ts.isoformat()


# ---------------------------------------------------------------------------
# Public tool functions
# ---------------------------------------------------------------------------
def get_7day_baseline(service: str | None = None) -> dict[str, float]:
    """
    Calculate the 7-day rolling average daily spend per AWS service.

    Args:
        service: If provided, return only this service's baseline.
                 If None, return all services.

    Returns:
        Dict mapping service name → average daily USD spend.
    """
    es = _get_es_client()
    query: dict[str, Any] = {
        "range": {
            "@timestamp": {
                "gte": _days_ago_utc(7),
                "lt": _midnight_utc(),
            }
        }
    }

    if service:
        query = {
            "bool": {
                "must": [
                    query,
                    {"term": {"aws.billing.ServiceName": service}},
                ]
            }
        }

    result = es.search(
        index=BILLING_INDEX,
        size=0,
        query=query,
        aggs={
            "per_service": {
                "terms": {"field": "aws.billing.ServiceName", "size": 50},
                "aggs": {
                    "daily": {
                        "date_histogram": {
                            "field": "@timestamp",
                            "calendar_interval": "day",
                        },
                        "aggs": {
                            "daily_cost": {
                                "sum": {"field": "aws.billing.UnblendedCost.amount"}
                            }
                        },
                    },
                    "avg_daily": {
                        "avg_bucket": {"buckets_path": "daily>daily_cost"}
                    },
                },
            }
        },
    )

    baselines = {}
    for bucket in result["aggregations"]["per_service"]["buckets"]:
        svc_name = bucket["key"]
        avg_daily = bucket["avg_daily"]["value"] or 0.0
        baselines[svc_name] = round(avg_daily, 2)

    return baselines


def get_todays_cost(service: str | None = None) -> dict[str, float]:
    """
    Get today's spend (since midnight UTC) per AWS service.

    Args:
        service: If provided, return only this service's cost.

    Returns:
        Dict mapping service name → today's USD spend.
    """
    es = _get_es_client()
    query: dict[str, Any] = {
        "range": {
            "@timestamp": {
                "gte": _midnight_utc(),
            }
        }
    }

    if service:
        query = {
            "bool": {
                "must": [
                    query,
                    {"term": {"aws.billing.ServiceName": service}},
                ]
            }
        }

    result = es.search(
        index=BILLING_INDEX,
        size=0,
        query=query,
        aggs={
            "per_service": {
                "terms": {"field": "aws.billing.ServiceName", "size": 50},
                "aggs": {
                    "total_cost": {
                        "sum": {"field": "aws.billing.UnblendedCost.amount"}
                    }
                },
            }
        },
    )

    costs = {}
    for bucket in result["aggregations"]["per_service"]["buckets"]:
        costs[bucket["key"]] = round(bucket["total_cost"]["value"] or 0.0, 2)

    return costs


def find_spike_services(threshold_pct: float) -> list[dict[str, Any]]:
    """
    Compare today's spend vs 7-day baseline per service.
    Return services where today exceeds baseline by >= threshold_pct.

    Args:
        threshold_pct: Percentage above baseline to flag (e.g. 25.0).

    Returns:
        List of anomaly dicts with service, today_usd, baseline_usd,
        delta_usd, pct_change, and team.
    """
    baselines = get_7day_baseline()
    todays = get_todays_cost()

    anomalies = []
    for service, today_cost in todays.items():
        baseline = baselines.get(service, 0.0)
        if baseline <= 0:
            continue

        pct_change = ((today_cost - baseline) / baseline) * 100
        if pct_change >= threshold_pct:
            anomalies.append({
                "service": service,
                "team": "unknown",  # populated from deploy events if available
                "today_usd": round(today_cost, 2),
                "baseline_usd": round(baseline, 2),
                "delta_usd": round(today_cost - baseline, 2),
                "pct_change": round(pct_change, 1),
            })

    # Sort by biggest dollar delta first
    anomalies.sort(key=lambda x: x["delta_usd"], reverse=True)
    logger.info("Found %d spike(s) above %.1f%% threshold", len(anomalies), threshold_pct)
    return anomalies


def get_cost_timeseries(service: str, hours: int = 48) -> list[dict[str, Any]]:
    """
    Get hourly cost data for a specific service over the past N hours.
    Used to pinpoint exactly when a spike started.

    Args:
        service: AWS service name (e.g. "Amazon EC2").
        hours: Number of past hours to fetch.

    Returns:
        List of {timestamp, cost_usd} dicts ordered chronologically.
    """
    es = _get_es_client()
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    result = es.search(
        index=BILLING_INDEX,
        size=0,
        query={
            "bool": {
                "must": [
                    {"range": {"@timestamp": {"gte": since.isoformat()}}},
                    {"term": {"aws.billing.ServiceName": service}},
                ]
            }
        },
        aggs={
            "hourly": {
                "date_histogram": {
                    "field": "@timestamp",
                    "fixed_interval": "1h",
                },
                "aggs": {
                    "hourly_cost": {
                        "sum": {"field": "aws.billing.UnblendedCost.amount"}
                    }
                },
            }
        },
    )

    timeseries = []
    for bucket in result["aggregations"]["hourly"]["buckets"]:
        timeseries.append({
            "timestamp": bucket["key_as_string"],
            "cost_usd": round(bucket["hourly_cost"]["value"] or 0.0, 2),
        })

    return timeseries


def find_deploys_near_spike(
    service: str, spike_start_iso: str, window_hours: int = 12
) -> list[dict[str, Any]]:
    """
    Find deployments within ±window_hours of spike_start_iso.

    Args:
        service: AWS service name to match.
        spike_start_iso: ISO 8601 timestamp of when the spike started.
        window_hours: Hours before/after to search.

    Returns:
        List of deploy event dicts.
    """
    es = _get_es_client()
    spike_time = datetime.fromisoformat(spike_start_iso.replace("Z", "+00:00"))
    window_start = spike_time - timedelta(hours=window_hours)
    window_end = spike_time + timedelta(hours=window_hours)

    result = es.search(
        index=DEPLOY_INDEX,
        size=20,
        query={
            "bool": {
                "must": [
                    {
                        "range": {
                            "@timestamp": {
                                "gte": window_start.isoformat(),
                                "lte": window_end.isoformat(),
                            }
                        }
                    }
                ]
            }
        },
        sort=[{"@timestamp": {"order": "desc"}}],
    )

    deploys = []
    for hit in result["hits"]["hits"]:
        src = hit["_source"]
        deploy_time = datetime.fromisoformat(
            src["@timestamp"].replace("Z", "+00:00")
        )
        hours_before = (spike_time - deploy_time).total_seconds() / 3600

        deploys.append({
            "service": src.get("service", "unknown"),
            "version": src.get("version", "unknown"),
            "team": src.get("team", "unknown"),
            "deployed_by": src.get("deployed_by", "unknown"),
            "commit_sha": src.get("commit_sha", "unknown"),
            "timestamp": src["@timestamp"],
            "hours_before_spike": round(hours_before, 1),
        })

    return deploys
