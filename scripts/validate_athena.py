#!/usr/bin/env python3
"""
scripts/validate_athena.py — Validate the Athena CUR setup before running the agent.

Run this after the CUR Glue crawler stack is deployed. Every check prints the
actual value it found, so a failure tells you what to change.

Usage:
  export ATHENA_DATABASE=athenacurcfn_aws_c_u_r
  export ATHENA_TABLE=aws_cur
  export ATHENA_OUTPUT_LOCATION=s3://aws-cur-reports-anurag/athena-results/
  python scripts/validate_athena.py
"""

import logging
import os
import sys

# Import the agent's own tools package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Agent"))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

PASS = "\033[92m✓ PASS\033[0m"
FAIL = "\033[91m✗ FAIL\033[0m"
WARN = "\033[93m! WARN\033[0m"


def check_env() -> bool:
    """ATHENA_DATABASE and ATHENA_TABLE are required; the output location may
    come from the workgroup instead."""
    from aws_athena_cur import (
        ATHENA_DATABASE, ATHENA_TABLE, ATHENA_OUTPUT_LOCATION,
        ATHENA_WORKGROUP, ATHENA_REGION,
    )

    print(f"       database={ATHENA_DATABASE}  table={ATHENA_TABLE}")
    print(f"       region={ATHENA_REGION}  workgroup={ATHENA_WORKGROUP}")
    print(f"       output={ATHENA_OUTPUT_LOCATION or '(from workgroup)'}")

    if not ATHENA_OUTPUT_LOCATION:
        import boto3
        try:
            wg = boto3.client("athena", region_name=ATHENA_REGION).get_work_group(
                WorkGroup=ATHENA_WORKGROUP
            )["WorkGroup"]
            location = wg["Configuration"].get("ResultConfiguration", {}).get("OutputLocation")
        except Exception as e:
            print(f"{FAIL}  Could not read workgroup {ATHENA_WORKGROUP}: {e}")
            return False
        if not location:
            print(f"{FAIL}  No ATHENA_OUTPUT_LOCATION set and workgroup "
                  f"'{ATHENA_WORKGROUP}' has none either. Queries will not start.")
            return False
        print(f"{PASS}  Output location from workgroup: {location}")
        return True

    print(f"{PASS}  Configuration present")
    return True


def check_schema():
    """Detect the CUR flavour and partition layout from Glue."""
    from aws_athena_cur import CostQueryError, detect_schema

    try:
        schema = detect_schema(refresh=True)
    except CostQueryError as e:
        print(f"{FAIL}  {e}")
        return None

    print(f"{PASS}  Detected {schema.flavor.upper()} CUR schema")
    print(f"       service={schema.service_col}")
    print(f"       cost={schema.cost_col}")
    print(f"       time={schema.time_col}")
    print(f"       partitions={schema.partition_style}")

    if schema.partition_style == "none":
        print(f"{WARN}  No partition keys — every query will scan the whole table.")
    if not schema.usage_filter:
        print(f"{WARN}  No line-item-type column: Tax/Credit/Refund rows will be "
              "counted as spend.")
    return schema


def check_partitions_registered() -> bool:
    """
    Confirm partitions are visible.

    A projected table (partition projection) has no registered partitions to list
    — SHOW PARTITIONS errors — so a successful data query is the real proof. A
    crawler-backed table that has never run, by contrast, lists nothing and reads
    nothing. Either way check_daily_costs below is the authority; this is advisory.
    """
    from aws_athena_cur import (
        ATHENA_DATABASE, ATHENA_TABLE, CostQueryError, run_athena_query,
    )

    try:
        rows = run_athena_query(f'SHOW PARTITIONS "{ATHENA_DATABASE}"."{ATHENA_TABLE}"')
    except CostQueryError:
        print(f"{PASS}  Table uses partition projection (no registered partitions "
              "to list) — data query below is the real check.")
        return True

    if not rows:
        print(f"{WARN}  No partitions listed. If this is a crawler-backed table, "
              "run the crawler; if it uses projection, ignore this.")
        return True

    values = [next(iter(r.values()), "") for r in rows]
    print(f"{PASS}  {len(values)} partition(s): {values[:6]}")
    return True


def check_daily_costs() -> bool:
    """Pull the real daily cost matrix — this exercises the generated SQL end to end."""
    from aws_athena_cur import CostQueryError, get_daily_costs

    try:
        daily = get_daily_costs()
    except CostQueryError as e:
        print(f"{FAIL}  {e}")
        return False

    if not daily:
        print(f"{FAIL}  Query succeeded but returned no rows — CUR data may not "
              "have landed for this window yet.")
        return False

    days = sorted({d for series in daily.values() for d in series})
    total = sum(sum(s.values()) for s in daily.values())
    print(f"{PASS}  {len(daily)} service(s) across {len(days)} day(s), ${total:,.2f} total")
    print(f"       days: {[d.isoformat() for d in days]}")

    top = sorted(
        ((svc, sum(s.values())) for svc, s in daily.items()),
        key=lambda x: x[1], reverse=True,
    )[:5]
    for svc, amount in top:
        print(f"       → {svc}: ${amount:,.2f}")
    return True


def check_spike_detection() -> bool:
    """Run the real detector. Zero spikes is a pass; an exception is not."""
    from aws_athena_cur import CostQueryError, find_spike_services

    try:
        spikes = find_spike_services(threshold_pct=25.0)
    except CostQueryError as e:
        print(f"{FAIL}  {e}")
        return False

    if not spikes:
        print(f"{PASS}  find_spike_services() ran; no anomalies above 25% "
              "(expected on a quiet account)")
        return True

    print(f"{PASS}  find_spike_services() found {len(spikes)} spike(s)")
    for s in spikes[:5]:
        change = "new cost source" if s.get("pct_change") is None else f"{s['pct_change']:+.1f}%"
        print(f"       → {s['service']} on {s['as_of']}: ${s['current_usd']:,.2f} "
              f"vs ${s['baseline_usd']:,.2f} baseline ({change})")
    return True


def main():
    print("=" * 64)
    print("  Athena CUR Validation")
    print("=" * 64)
    print()

    results: dict[str, bool] = {}

    results["config"] = check_env()
    if not results["config"]:
        sys.exit(1)

    print()
    schema = check_schema()
    results["schema"] = schema is not None
    if not schema:
        print("\nCannot continue without a readable table.")
        sys.exit(1)

    print()
    results["partitions"] = check_partitions_registered()

    print()
    results["data"] = check_daily_costs()

    print()
    results["spikes"] = check_spike_detection() if results["data"] else False

    print()
    print("=" * 64)
    passed = sum(1 for v in results.values() if v)
    print(f"  {passed}/{len(results)} checks passed")
    print("=" * 64)

    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
