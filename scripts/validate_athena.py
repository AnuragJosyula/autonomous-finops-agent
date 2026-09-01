#!/usr/bin/env python3
"""
scripts/validate_athena.py — Validate Athena CUR setup before running the agent.

Checks:
  1. Database and table exist
  2. FOCUS 1.4 vs legacy CUR columns detected
  3. Sample cost query returns data
  4. find_spike_services() runs end-to-end

Usage:
  export ATHENA_DATABASE=athenacurcfn_aws_cur
  export ATHENA_TABLE=aws_cur
  export ATHENA_OUTPUT_LOCATION=s3://aws-cur-reports-anurag/athena-results/
  python scripts/validate_athena.py
"""

import os
import sys

# Add project root to path so we can import tools
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aws_native_plug_and_play"))

import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

PASS = "\033[92m✓ PASS\033[0m"
FAIL = "\033[91m✗ FAIL\033[0m"

FOCUS_COLUMNS = {"servicename", "billedcost", "chargeperiodstart", "chargeperiodend", "billingaccountid"}
LEGACY_COLUMNS = {"line_item_product_code", "line_item_unblended_cost", "line_item_usage_start_date"}


def check_env():
    """Check required env vars are set."""
    db = os.environ.get("ATHENA_DATABASE")
    table = os.environ.get("ATHENA_TABLE")
    output = os.environ.get("ATHENA_OUTPUT_LOCATION")

    if not all([db, table, output]):
        print(f"{FAIL}  Missing env vars. Need: ATHENA_DATABASE, ATHENA_TABLE, ATHENA_OUTPUT_LOCATION")
        print(f"       Got: db={db}, table={table}, output={output}")
        return False
    print(f"{PASS}  Env vars set: db={db}, table={table}")
    return True


def check_table_exists():
    """Verify the Athena table exists via SHOW TABLES."""
    from tools.aws_athena_cur import run_athena_query, ATHENA_DATABASE, ATHENA_TABLE

    records = run_athena_query(f'SHOW TABLES IN "{ATHENA_DATABASE}"')
    if not records:
        print(f"{FAIL}  Could not list tables in database '{ATHENA_DATABASE}'")
        return False

    # SHOW TABLES returns rows with a single 'tab_name' column
    table_names = set()
    for r in records:
        for v in r.values():
            table_names.add(v.lower())

    expected = ATHENA_TABLE.lower()
    if expected in table_names:
        print(f"{PASS}  Table '{ATHENA_TABLE}' found in database '{ATHENA_DATABASE}'")
        return True
    else:
        print(f"{FAIL}  Table '{ATHENA_TABLE}' NOT found. Available: {sorted(table_names)}")
        return False


def check_columns():
    """Detect FOCUS 1.4 vs legacy CUR columns."""
    from tools.aws_athena_cur import run_athena_query, ATHENA_DATABASE, ATHENA_TABLE

    records = run_athena_query(f'SHOW COLUMNS IN "{ATHENA_DATABASE}"."{ATHENA_TABLE}"')
    if not records:
        print(f"{FAIL}  Could not list columns")
        return None

    col_names = set()
    for r in records:
        for v in r.values():
            col_names.add(v.lower().strip())

    has_focus = FOCUS_COLUMNS.issubset(col_names)
    has_legacy = LEGACY_COLUMNS.issubset(col_names)

    if has_focus:
        print(f"{PASS}  FOCUS 1.4 columns detected (ServiceName, BilledCost, etc.)")
    elif has_legacy:
        print(f"{PASS}  Legacy CUR columns detected (line_item_product_code, etc.)")
    else:
        print(f"{FAIL}  Neither FOCUS nor legacy CUR columns found")
        print(f"       Columns found: {sorted(list(col_names)[:20])}...")

    return "focus" if has_focus else ("legacy" if has_legacy else None)


def check_sample_query():
    """Run a simple cost query to verify data exists."""
    from tools.aws_athena_cur import run_athena_query, ATHENA_DATABASE, ATHENA_TABLE

    query = f"""
        SELECT COUNT(*) AS row_count,
               SUM(CAST(COALESCE(BilledCost, line_item_unblended_cost) AS double)) AS total_cost
        FROM "{ATHENA_DATABASE}"."{ATHENA_TABLE}"
        LIMIT 1
    """
    records = run_athena_query(query)
    if not records:
        print(f"{FAIL}  Sample query returned no results (CUR data may not have landed yet)")
        return False

    row_count = records[0].get("row_count", "0")
    total_cost = records[0].get("total_cost", "0")
    print(f"{PASS}  Sample query: {row_count} rows, ${float(total_cost):,.2f} total cost")
    return int(row_count) > 0


def check_spike_detection():
    """Run find_spike_services() end-to-end."""
    from tools.aws_athena_cur import find_spike_services

    try:
        spikes = find_spike_services(threshold_pct=25.0)
        print(f"{PASS}  find_spike_services() returned {len(spikes)} spike(s)")
        for s in spikes[:5]:
            print(f"       → {s['service']}: ${s['today_usd']:.2f} today vs ${s['baseline_usd']:.2f} baseline ({s['pct_change']:+.1f}%)")
        return True
    except Exception as e:
        print(f"{FAIL}  find_spike_services() raised: {e}")
        return False


def main():
    print("=" * 60)
    print("  Athena CUR Validation")
    print("=" * 60)
    print()

    results = {}

    results["env"] = check_env()
    if not results["env"]:
        sys.exit(1)

    print()
    results["table"] = check_table_exists()

    print()
    results["columns"] = check_columns() is not None

    print()
    results["data"] = check_sample_query()

    print()
    results["spike"] = check_spike_detection()

    print()
    print("=" * 60)
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"  Results: {passed}/{total} checks passed")
    print("=" * 60)

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
