"""
tools/aws_athena_cur.py — Enterprise AWS Cost Querying via Athena & CUR (Cost & Usage Report / FOCUS).

Queries AWS CUR data in S3 via Athena SQL execution.
- Cost: ~$0.005 per GB scanned (vs $0.01 per Cost Explorer API call).
- Granularity: Resource ARNs, pricing models, and FOCUS schema compatibility.
- Scale: Supports AWS Organizations multi-account consolidated billing.
"""

import os
import re
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3

logger = logging.getLogger(__name__)

ATHENA_DATABASE = os.environ.get("ATHENA_DATABASE", "athenacurcfn_aws_cur")
ATHENA_TABLE = os.environ.get("ATHENA_TABLE", "aws_cur")
ATHENA_OUTPUT_LOCATION = os.environ.get("ATHENA_OUTPUT_LOCATION", "s3://aws-athena-query-results-finops/")
ATHENA_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Regex: only allow alphanumeric, spaces, hyphens, slashes, dots (valid AWS service names)
_SAFE_SERVICE_RE = re.compile(r"^[A-Za-z0-9 \-_./]+$")


def _sanitize_service(service: str) -> str:
    """Validate and sanitize a service name to prevent SQL injection."""
    if not _SAFE_SERVICE_RE.match(service):
        raise ValueError(f"Invalid service name: {service!r}")
    return service


def _get_athena_client():
    """Return a boto3 Athena client."""
    return boto3.client("athena", region_name=ATHENA_REGION)


def run_athena_query(query: str, params: list[str] | None = None, timeout_seconds: int = 30) -> list[dict[str, Any]]:
    """
    Execute an Athena SQL query synchronously and return results as dict records.
    Supports parameterized queries via ExecutionParameters to prevent SQL injection.
    """
    client = _get_athena_client()
    execution_id = None
    try:
        start_kwargs = {
            "QueryString": query,
            "QueryExecutionContext": {"Database": ATHENA_DATABASE},
            "ResultConfiguration": {"OutputLocation": ATHENA_OUTPUT_LOCATION},
        }
        if params:
            start_kwargs["ExecutionParameters"] = params

        response = client.start_query_execution(**start_kwargs)
        execution_id = response["QueryExecutionId"]

        # Poll with exponential backoff (0.5s, 1s, 2s, 4s, capped at 5s)
        start_time = time.time()
        backoff = 0.5
        succeeded = False
        while time.time() - start_time < timeout_seconds:
            status_resp = client.get_query_execution(QueryExecutionId=execution_id)
            state = status_resp["QueryExecution"]["Status"]["State"]
            if state == "SUCCEEDED":
                succeeded = True
                break
            elif state in ["FAILED", "CANCELLED"]:
                reason = status_resp["QueryExecution"]["Status"].get("StateChangeReason", "Unknown")
                logger.error("Athena query failed (%s): %s", state, reason)
                return []
            time.sleep(backoff)
            backoff = min(backoff * 2, 5.0)

        if not succeeded:
            # Timeout — cancel the running query to avoid unnecessary costs
            logger.warning("Athena query timed out after %ds, cancelling: %s", timeout_seconds, execution_id)
            try:
                client.stop_query_execution(QueryExecutionId=execution_id)
            except Exception:
                logger.warning("Failed to cancel query %s", execution_id)
            return []

        # Fetch all result pages (default page is 1000 rows)
        all_rows: list[dict] = []
        headers: list[str] = []
        next_token = None
        first_page = True

        while True:
            get_kwargs: dict[str, Any] = {"QueryExecutionId": execution_id}
            if next_token:
                get_kwargs["NextToken"] = next_token

            results_resp = client.get_query_results(**get_kwargs)
            rows = results_resp.get("ResultSet", {}).get("Rows", [])

            if first_page:
                if not rows:
                    return []
                headers = [col.get("VarCharValue", "") for col in rows[0]["Data"]]
                data_rows = rows[1:]  # skip header row
                first_page = False
            else:
                data_rows = rows

            for row in data_rows:
                values = [col.get("VarCharValue", "") for col in row["Data"]]
                all_rows.append(dict(zip(headers, values)))

            next_token = results_resp.get("NextToken")
            if not next_token:
                break

        return all_rows
    except Exception as e:
        logger.error("Failed to run Athena query: %s", e)
        return []


def get_7day_baseline(service: str | None = None) -> dict[str, float]:
    """
    Calculate 7-day rolling average daily spend per service using FOCUS 1.4 schema (BilledCost, ServiceName, ChargePeriodStart).
    """
    today = datetime.now(timezone.utc).date()
    seven_days_ago = today - timedelta(days=7)

    query = f"""
        SELECT 
            COALESCE(ServiceName, line_item_product_code) AS service,
            SUM(CAST(COALESCE(BilledCost, line_item_unblended_cost) AS double)) / 7.0 AS daily_avg_usd
        FROM "{ATHENA_DATABASE}"."{ATHENA_TABLE}"
        WHERE COALESCE(ChargePeriodStart, line_item_usage_start_date) >= DATE '{seven_days_ago.strftime("%Y-%m-%d")}'
          AND COALESCE(ChargePeriodStart, line_item_usage_start_date) < DATE '{today.strftime("%Y-%m-%d")}'
        GROUP BY COALESCE(ServiceName, line_item_product_code)
        HAVING SUM(CAST(COALESCE(BilledCost, line_item_unblended_cost) AS double)) > 0
    """
    records = run_athena_query(query)
    baselines = {}
    for r in records:
        svc = r.get("service", "Unknown")
        try:
            avg_cost = float(r.get("daily_avg_usd", 0.0))
            baselines[svc] = round(avg_cost, 2)
        except ValueError:
            continue

    if service:
        return {service: baselines.get(service, 0.0)}
    return baselines


def get_todays_cost(service: str | None = None) -> dict[str, float]:
    """
    Get today's spend per service using FOCUS 1.4 schema.
    """
    today = datetime.now(timezone.utc).date()

    query = f"""
        SELECT 
            COALESCE(ServiceName, line_item_product_code) AS service,
            SUM(CAST(COALESCE(BilledCost, line_item_unblended_cost) AS double)) AS todays_cost_usd
        FROM "{ATHENA_DATABASE}"."{ATHENA_TABLE}"
        WHERE COALESCE(ChargePeriodStart, line_item_usage_start_date) >= DATE '{today.strftime("%Y-%m-%d")}'
        GROUP BY COALESCE(ServiceName, line_item_product_code)
    """
    records = run_athena_query(query)
    costs = {}
    for r in records:
        svc = r.get("service", "Unknown")
        try:
            cost = float(r.get("todays_cost_usd", 0.0))
            costs[svc] = round(cost, 2)
        except ValueError:
            continue

    if service:
        return {service: costs.get(service, 0.0)}
    return costs


def find_spike_services(threshold_pct: float = 25.0) -> list[dict[str, Any]]:
    """
    Compare today's AWS spend vs 7-day baseline using FOCUS 1.4 compliant Athena data.
    Flag services exceeding baseline by >= threshold_pct.
    """
    baselines = get_7day_baseline()
    todays = get_todays_cost()

    anomalies = []
    for service, today_cost in todays.items():
        baseline = baselines.get(service, 0.0)
        if baseline <= 1.0:  # Ignore spend under $1/day
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
    logger.info("Athena FOCUS CUR flagged %d spike(s) above %.1f%%", len(anomalies), threshold_pct)
    return anomalies


def get_cost_timeseries(service: str, hours: int = 48) -> list[dict[str, Any]]:
    """
    Get hourly cost data for a specific service using FOCUS 1.4 schema.
    Uses parameterized query to prevent SQL injection via service name.
    """
    service = _sanitize_service(service)
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(hours=hours)

    query = f"""
        SELECT 
            DATE_TRUNC('hour', COALESCE(ChargePeriodStart, line_item_usage_start_date)) AS hour_ts,
            SUM(CAST(COALESCE(BilledCost, line_item_unblended_cost) AS double)) AS hourly_cost
        FROM "{ATHENA_DATABASE}"."{ATHENA_TABLE}"
        WHERE COALESCE(ServiceName, line_item_product_code) = ?
          AND COALESCE(ChargePeriodStart, line_item_usage_start_date) >= TIMESTAMP '{start_time.strftime("%Y-%m-%d %H:%M:%S")}'
        GROUP BY DATE_TRUNC('hour', COALESCE(ChargePeriodStart, line_item_usage_start_date))
        ORDER BY hour_ts ASC
    """
    records = run_athena_query(query, params=[service])
    timeseries = []
    for r in records:
        try:
            timeseries.append({
                "timestamp": r.get("hour_ts", ""),
                "cost_usd": round(float(r.get("hourly_cost", 0.0)), 2),
            })
        except ValueError:
            continue
    return timeseries
