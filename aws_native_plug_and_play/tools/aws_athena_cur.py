"""
tools/aws_athena_cur.py — AWS cost querying via Athena over Cost & Usage Report data.

Supports legacy CUR (snake_case columns) and FOCUS 1.x / CUR 2.0 exports. The
table schema is detected once from the Glue Data Catalog and cached, so every
query references only columns that actually exist — a table has one schema, not
both, and referencing an absent column fails at query analysis time in Trino.

Failures raise CostQueryError rather than returning empty results. The agent has
to be able to tell "no anomalies" apart from "could not query".
"""

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import boto3

logger = logging.getLogger(__name__)

ATHENA_DATABASE = os.environ.get("ATHENA_DATABASE", "athenacurcfn_aws_c_u_r")
ATHENA_TABLE = os.environ.get("ATHENA_TABLE", "aws_cur")
# Empty by default: fall back to the workgroup's own output location if it has one.
ATHENA_OUTPUT_LOCATION = os.environ.get("ATHENA_OUTPUT_LOCATION", "")
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "primary")

# AWS_REGION is reserved in Lambda and cannot be set on a function's environment,
# so FINOPS_AWS_REGION takes precedence and lets the data plane live elsewhere.
ATHENA_REGION = os.environ.get("FINOPS_AWS_REGION") or os.environ.get("AWS_REGION", "us-east-1")

BASELINE_DAYS = 7
MIN_BASELINE_USD = float(os.environ.get("MIN_BASELINE_USD", "1.0"))
# A service with no meaningful 7-day baseline can't have a percentage spike — a
# new or dormant cost source instead trips an absolute-dollar floor. This is the
# only way a brand-new service (baseline $0) is ever flagged.
NEW_SERVICE_USD = float(os.environ.get("NEW_SERVICE_USD", "5.0"))
QUERY_TIMEOUT_SECONDS = int(os.environ.get("ATHENA_TIMEOUT_SECONDS", "60"))

# Valid AWS service names: alphanumerics, spaces, and a little punctuation.
_SAFE_SERVICE_RE = re.compile(r"^[A-Za-z0-9 \-_./()]+$")

# Minimum column sets that identify each export flavour.
_LEGACY_COLUMNS = {
    "line_item_product_code",
    "line_item_unblended_cost",
    "line_item_usage_start_date",
}
_FOCUS_COLUMNS = {"servicename", "billedcost", "chargeperiodstart"}

# Legacy CUR mixes Tax / Credit / Refund / SavingsPlanNegation rows in with usage.
# Summing them all together is not what anyone means by "spend".
_LEGACY_USAGE_TYPES = ("Usage", "DiscountedUsage", "SavingsPlanCoveredUsage")


class CostQueryError(RuntimeError):
    """Raised when cost data could not be retrieved. Never means 'no spend'."""


@dataclass(frozen=True)
class CurSchema:
    """Column and partition layout of the CUR table, detected from Glue."""

    flavor: str  # "legacy" | "focus"
    service_col: str
    cost_col: str
    time_col: str
    usage_filter: str | None
    partition_style: str  # "year_month" | "billing_period" | "none"


_schema_cache: CurSchema | None = None


def _sanitize_service(service: str) -> str:
    """Validate a service name before it reaches a query."""
    if not service or not _SAFE_SERVICE_RE.match(service):
        raise ValueError(f"Invalid service name: {service!r}")
    return service


def _get_athena_client():
    return boto3.client("athena", region_name=ATHENA_REGION)


def detect_schema(refresh: bool = False) -> CurSchema:
    """
    Read the CUR table definition from Glue and work out which column names to use.

    Cached for the life of the process — the schema does not change between runs,
    so on Lambda this costs one Glue call per cold start.
    """
    global _schema_cache
    if _schema_cache is not None and not refresh:
        return _schema_cache

    glue = boto3.client("glue", region_name=ATHENA_REGION)
    try:
        table = glue.get_table(DatabaseName=ATHENA_DATABASE, Name=ATHENA_TABLE)["Table"]
    except Exception as e:
        raise CostQueryError(
            f"Cannot read Glue table {ATHENA_DATABASE}.{ATHENA_TABLE} in {ATHENA_REGION}: {e}. "
            "Has the CUR Glue crawler stack been deployed, and do ATHENA_DATABASE / "
            "ATHENA_TABLE match it?"
        ) from e

    columns = {c["Name"].lower() for c in table["StorageDescriptor"].get("Columns", [])}
    partitions = [p["Name"].lower() for p in table.get("PartitionKeys", [])]

    if "year" in partitions and "month" in partitions:
        partition_style = "year_month"
    elif "billing_period" in partitions:
        partition_style = "billing_period"
    else:
        partition_style = "none"
        logger.warning(
            "CUR table has no recognised partition keys (%s) — queries will scan the "
            "whole table.", partitions or "none",
        )

    if _LEGACY_COLUMNS.issubset(columns):
        usage_filter = None
        if "line_item_line_item_type" in columns:
            types = ", ".join(f"'{t}'" for t in _LEGACY_USAGE_TYPES)
            usage_filter = f"line_item_line_item_type IN ({types})"
        schema = CurSchema(
            flavor="legacy",
            service_col="line_item_product_code",
            cost_col="line_item_unblended_cost",
            time_col="line_item_usage_start_date",
            usage_filter=usage_filter,
            partition_style=partition_style,
        )
    elif _FOCUS_COLUMNS.issubset(columns):
        usage_filter = "chargecategory = 'Usage'" if "chargecategory" in columns else None
        schema = CurSchema(
            flavor="focus",
            service_col="servicename",
            cost_col="billedcost",
            time_col="chargeperiodstart",
            usage_filter=usage_filter,
            partition_style=partition_style,
        )
    else:
        sample = sorted(columns)[:15]
        raise CostQueryError(
            f"{ATHENA_DATABASE}.{ATHENA_TABLE} matches neither legacy CUR nor FOCUS. "
            f"Expected one of {sorted(_LEGACY_COLUMNS)} or {sorted(_FOCUS_COLUMNS)}. "
            f"Found columns starting with: {sample}"
        )

    logger.info(
        "CUR schema: %s | service=%s cost=%s time=%s | partitions=%s",
        schema.flavor, schema.service_col, schema.cost_col, schema.time_col,
        schema.partition_style,
    )
    _schema_cache = schema
    return schema


def _partition_predicate(schema: CurSchema, start: date, end: date) -> str:
    """
    Build a partition filter covering every month the window touches.

    Without this every query full-scans the CUR. Crawler-created partitions are
    usually unpadded (month=9), but some are padded (month=09), so match both.
    """
    if schema.partition_style == "none":
        return "TRUE"

    months: list[tuple[int, int]] = []
    cursor = date(start.year, start.month, 1)
    final = date(end.year, end.month, 1)
    while cursor <= final:
        months.append((cursor.year, cursor.month))
        cursor = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )

    if schema.partition_style == "year_month":
        clauses = [f"(year = '{y}' AND month IN ('{m}', '{m:02d}'))" for y, m in months]
        return "(" + " OR ".join(clauses) + ")"

    periods = ", ".join(f"'{y}-{m:02d}'" for y, m in months)
    return f"billing_period IN ({periods})"


def run_athena_query(
    query: str,
    params: list[str] | None = None,
    timeout_seconds: int | None = None,
) -> list[dict[str, Any]]:
    """
    Execute an Athena query synchronously and return the rows as dicts.

    Uses ExecutionParameters for value substitution (Athena engine v3) so service
    names never reach the SQL text. Raises CostQueryError on any failure.
    """
    timeout_seconds = timeout_seconds or QUERY_TIMEOUT_SECONDS
    client = _get_athena_client()

    start_kwargs: dict[str, Any] = {
        "QueryString": query,
        "QueryExecutionContext": {"Database": ATHENA_DATABASE},
        "WorkGroup": ATHENA_WORKGROUP,
    }
    if ATHENA_OUTPUT_LOCATION:
        start_kwargs["ResultConfiguration"] = {"OutputLocation": ATHENA_OUTPUT_LOCATION}
    if params:
        start_kwargs["ExecutionParameters"] = params

    try:
        execution_id = client.start_query_execution(**start_kwargs)["QueryExecutionId"]
    except Exception as e:
        raise CostQueryError(
            f"Could not start Athena query: {e}. If this mentions an output location, "
            "set ATHENA_OUTPUT_LOCATION or configure one on the workgroup."
        ) from e

    deadline = time.time() + timeout_seconds
    backoff = 0.5
    state = "QUEUED"
    succeeded = False
    while time.time() < deadline:
        status = client.get_query_execution(
            QueryExecutionId=execution_id
        )["QueryExecution"]["Status"]
        state = status["State"]
        if state == "SUCCEEDED":
            succeeded = True
            break
        if state in ("FAILED", "CANCELLED"):
            reason = status.get("StateChangeReason", "unknown")
            raise CostQueryError(f"Athena query {state}: {reason}")
        time.sleep(backoff)
        backoff = min(backoff * 2, 5.0)

    if not succeeded:
        try:
            client.stop_query_execution(QueryExecutionId=execution_id)
        except Exception:
            logger.warning("Could not cancel timed-out query %s", execution_id)
        raise CostQueryError(
            f"Athena query timed out after {timeout_seconds}s (last state: {state})"
        )

    rows: list[dict[str, Any]] = []
    headers: list[str] = []
    next_token = None
    first_page = True
    try:
        while True:
            kwargs: dict[str, Any] = {"QueryExecutionId": execution_id}
            if next_token:
                kwargs["NextToken"] = next_token
            page = client.get_query_results(**kwargs)
            page_rows = page.get("ResultSet", {}).get("Rows", [])

            if first_page:
                if not page_rows:
                    return []
                headers = [c.get("VarCharValue", "") for c in page_rows[0]["Data"]]
                page_rows = page_rows[1:]
                first_page = False

            for row in page_rows:
                values = [c.get("VarCharValue", "") for c in row["Data"]]
                rows.append(dict(zip(headers, values)))

            next_token = page.get("NextToken")
            if not next_token:
                break
    except Exception as e:
        raise CostQueryError(f"Could not read Athena results for {execution_id}: {e}") from e

    return rows


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_daily_costs(days: int = BASELINE_DAYS + 2) -> dict[str, dict[date, float]]:
    """
    Return {service: {usage_date: cost_usd}} for the last `days` days.

    One query serves both the baseline and the comparison day, which halves the
    data scanned versus querying them separately.
    """
    schema = detect_schema()
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)

    where = [
        _partition_predicate(schema, start, today),
        f"{schema.time_col} >= TIMESTAMP '{start} 00:00:00'",
        f"{schema.time_col} < TIMESTAMP '{today + timedelta(days=1)} 00:00:00'",
    ]
    if schema.usage_filter:
        where.append(schema.usage_filter)

    query = f"""
        SELECT {schema.service_col} AS service,
               DATE_TRUNC('day', {schema.time_col}) AS usage_day,
               SUM({schema.cost_col}) AS cost_usd
        FROM "{ATHENA_DATABASE}"."{ATHENA_TABLE}"
        WHERE {' AND '.join(where)}
        GROUP BY {schema.service_col}, DATE_TRUNC('day', {schema.time_col})
    """

    costs: dict[str, dict[date, float]] = {}
    for row in run_athena_query(query):
        service = row.get("service") or "Unknown"
        cost = _to_float(row.get("cost_usd"))
        raw_day = (row.get("usage_day") or "")[:10]
        if cost is None or not raw_day:
            continue
        try:
            day = date.fromisoformat(raw_day)
        except ValueError:
            continue
        costs.setdefault(service, {})[day] = cost

    return costs


def find_spike_services(threshold_pct: float = 25.0) -> list[dict[str, Any]]:
    """
    Compare the most recent complete day of CUR data against the 7 days before it.

    CUR is delivered on a lag and restated during the month, so the current day is
    always partial — comparing it against a full-day baseline guarantees false
    negatives.
    """
    daily = get_daily_costs()
    if not daily:
        raise CostQueryError(
            "CUR query returned no rows. Data may not have landed yet, or the "
            "partitions may not be registered — run the Glue crawler."
        )

    today = datetime.now(timezone.utc).date()
    all_days = sorted({d for series in daily.values() for d in series})
    complete_days = [d for d in all_days if d < today]
    if not complete_days:
        raise CostQueryError(
            "CUR contains no complete day yet (only partial data for today). "
            "Wait for the next CUR delivery."
        )

    target_day = max(complete_days)
    window_start = target_day - timedelta(days=BASELINE_DAYS)
    baseline_days = [d for d in complete_days if window_start <= d < target_day]
    if not baseline_days:
        raise CostQueryError(
            f"Only one day of CUR data ({target_day}) — need at least two to build "
            "a baseline."
        )

    divisor = len(baseline_days)
    if divisor < BASELINE_DAYS:
        logger.warning(
            "Baseline uses %d day(s), not %d — CUR history is still filling in.",
            divisor, BASELINE_DAYS,
        )

    anomalies = []
    for service, series in daily.items():
        current = series.get(target_day, 0.0)
        baseline = sum(series.get(d, 0.0) for d in baseline_days) / divisor

        if baseline > MIN_BASELINE_USD:
            # Established service: flag a percentage jump above its own baseline.
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
                    "is_new_service": False,
                })
        elif current >= NEW_SERVICE_USD:
            # New or dormant service: no baseline to take a percentage of, so a
            # meaningful absolute spend is the anomaly.
            anomalies.append({
                "service": service,
                "team": "AWS Account",
                "as_of": target_day.isoformat(),
                "current_usd": round(current, 2),
                "baseline_usd": round(baseline, 2),
                "delta_usd": round(current - baseline, 2),
                "pct_change": None,
                "is_new_service": True,
            })

    anomalies.sort(key=lambda a: a["delta_usd"], reverse=True)
    logger.info(
        "Athena CUR: %d spike(s) above %.1f%% for %s vs %d-day baseline",
        len(anomalies), threshold_pct, target_day, divisor,
    )
    return anomalies


def get_cost_timeseries(service: str, hours: int = 48) -> list[dict[str, Any]]:
    """Hourly cost for one service over the past N hours, to pinpoint the spike start."""
    service = _sanitize_service(service)
    schema = detect_schema()

    now = datetime.now(timezone.utc)
    start_time = now - timedelta(hours=hours)

    where = [
        _partition_predicate(schema, start_time.date(), now.date()),
        f"{schema.service_col} = ?",
        f"{schema.time_col} >= TIMESTAMP '{start_time.strftime('%Y-%m-%d %H:00:00')}'",
    ]
    if schema.usage_filter:
        where.append(schema.usage_filter)

    query = f"""
        SELECT DATE_TRUNC('hour', {schema.time_col}) AS hour_ts,
               SUM({schema.cost_col}) AS hourly_cost
        FROM "{ATHENA_DATABASE}"."{ATHENA_TABLE}"
        WHERE {' AND '.join(where)}
        GROUP BY DATE_TRUNC('hour', {schema.time_col})
        ORDER BY hour_ts ASC
    """

    timeseries = []
    for row in run_athena_query(query, params=[service]):
        cost = _to_float(row.get("hourly_cost"))
        if cost is None:
            continue
        timeseries.append({
            "timestamp": row.get("hour_ts", ""),
            "cost_usd": round(cost, 2),
        })
    return timeseries
