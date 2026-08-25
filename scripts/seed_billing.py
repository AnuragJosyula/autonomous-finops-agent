#!/usr/bin/env python3
"""
scripts/seed_billing.py — Seed Elasticsearch with demo billing + deploy data.

Seeds 7 days of baseline EC2 costs (~$25/hr) plus today's spike
(~$44/hr starting at 17:00 UTC) so the agent detects an anomaly.
Also seeds one deploy event near the spike to allow correlation.

Uses the same field schema as the Elastic AWS Billing integration
(aws.billing.ServiceName, aws.billing.UnblendedCost.amount) so the
agent code is identical whether reading real or seeded data.

Usage:
    python scripts/seed_billing.py \
        --es-url https://your-project.es.us-east-1.aws.elastic.cloud \
        --api-key YOUR_SUPERUSER_API_KEY
"""

import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone

import urllib.request
import urllib.error


def post_doc(es_url: str, api_key: str, index: str, doc: dict) -> bool:
    """Index a single document into Elasticsearch."""
    url = f"{es_url}/{index}/_doc"
    data = json.dumps(doc).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"ApiKey {api_key}",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 201)
    except urllib.error.HTTPError as e:
        print(f"  ERROR {e.code}: {e.read().decode()[:200]}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="Seed aws-billing-* demo data")
    parser.add_argument("--es-url", required=True, help="Elasticsearch URL")
    parser.add_argument("--api-key", required=True, help="Base64 API key (superuser)")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    services = [
        {"name": "Amazon EC2", "team": "checkout-team", "base_hourly": 25.0},
        {"name": "Amazon S3", "team": "data-team", "base_hourly": 8.0},
        {"name": "Amazon RDS", "team": "backend-team", "base_hourly": 15.0},
    ]

    total_docs = 0
    errors = 0

    # -----------------------------------------------------------------------
    # Seed 7 days of baseline billing data (normal costs)
    # -----------------------------------------------------------------------
    print("=== Seeding 7-day baseline billing data ===")
    for day_offset in range(7, 0, -1):
        day_start = today - timedelta(days=day_offset)
        for hour in range(24):
            ts = day_start + timedelta(hours=hour)
            for svc in services:
                # Add some random variation (±10%) to make it realistic
                jitter = random.uniform(0.9, 1.1)
                cost = svc["base_hourly"] * jitter

                doc = {
                    "@timestamp": ts.isoformat(),
                    "aws.billing.ServiceName": svc["name"],
                    "aws.billing.UnblendedCost.amount": round(cost, 2),
                    "cloud.account.id": "123456789012",
                    "cloud.provider": "aws",
                }

                index = f"metrics-aws.billing-{ts.strftime('%Y.%m.%d')}"
                ok = post_doc(args.es_url, args.api_key, index, doc)
                total_docs += 1
                if not ok:
                    errors += 1

        print(f"  Day -{day_offset}: seeded {24 * len(services)} hourly records")

    # -----------------------------------------------------------------------
    # Seed today's data: normal until 17:00, then spike for EC2
    # -----------------------------------------------------------------------
    print("=== Seeding today's data (with EC2 spike at 17:00 UTC) ===")
    current_hour = now.hour
    for hour in range(current_hour + 1):
        ts = today + timedelta(hours=hour)
        for svc in services:
            jitter = random.uniform(0.9, 1.1)

            if svc["name"] == "Amazon EC2" and hour >= 17:
                # Spike: ~$44/hr instead of ~$25/hr (76% increase)
                cost = 44.0 * jitter
            else:
                cost = svc["base_hourly"] * jitter

            doc = {
                "@timestamp": ts.isoformat(),
                "aws.billing.ServiceName": svc["name"],
                "aws.billing.UnblendedCost.amount": round(cost, 2),
                "cloud.account.id": "123456789012",
                "cloud.provider": "aws",
            }

            index = f"metrics-aws.billing-{ts.strftime('%Y.%m.%d')}"
            ok = post_doc(args.es_url, args.api_key, index, doc)
            total_docs += 1
            if not ok:
                errors += 1

    print(f"  Today: seeded {(current_hour + 1) * len(services)} hourly records")

    # -----------------------------------------------------------------------
    # Seed one deploy event (3 hours before the spike)
    # -----------------------------------------------------------------------
    print("=== Seeding deploy event ===")
    deploy_time = today + timedelta(hours=14)  # Deploy at 14:00, spike at 17:00
    deploy_doc = {
        "@timestamp": deploy_time.isoformat(),
        "service": "checkout",
        "version": "v2.3.1",
        "team": "checkout-team",
        "deployed_by": "alice@acme.com",
        "commit_sha": "a3f9c12d",
        "description": "Add HPA autoscaling to checkout pods",
    }

    deploy_index = f"deploy-events-{deploy_time.strftime('%Y.%m.%d')}"
    ok = post_doc(args.es_url, args.api_key, deploy_index, deploy_doc)
    total_docs += 1
    if not ok:
        errors += 1

    print(f"  Deploy: checkout v2.3.1 at {deploy_time.isoformat()}")

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    print(f"\n=== Done ===")
    print(f"  Total documents: {total_docs}")
    print(f"  Errors: {errors}")
    if errors == 0:
        print("  ✅ All documents indexed successfully")
    else:
        print(f"  ⚠️  {errors} document(s) failed to index")


if __name__ == "__main__":
    main()
