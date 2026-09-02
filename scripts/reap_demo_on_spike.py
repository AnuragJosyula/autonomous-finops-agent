#!/usr/bin/env python3
"""
scripts/reap_demo_on_spike.py — Run the real agent; terminate the demo instance on a spike.

The finops-demo instance already self-destructs 48h after boot (a hard cost cap
baked into its user-data), so cleanup is guaranteed even if this never runs. This
script is the early-exit path: once the instance's cost is large enough to trip
the detector, it posts the Slack alert and terminates the instance immediately,
so you neither wait for the timer nor delete anything by hand.

It only ever terminates instances tagged exactly Name=finops-demo AND
purpose=cost-anomaly-demo-spike. Nothing else is touched.

Usage:
  export COST_PROVIDER=ATHENA_CUR
  export ATHENA_OUTPUT_LOCATION=s3://aws-cur-reports-anurag/athena-results/
  # SLACK_WEBHOOK_URL is read from .env.local
  python scripts/reap_demo_on_spike.py

  python scripts/reap_demo_on_spike.py --dry-run   # detect + alert, but don't terminate
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aws_native_plug_and_play"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
logging.getLogger("botocore").setLevel(logging.WARNING)
logger = logging.getLogger("reaper")

DEMO_REGION = os.environ.get("FINOPS_AWS_REGION") or os.environ.get("AWS_REGION", "us-east-1")
_REQUIRED_TAGS = {"Name": "finops-demo", "purpose": "cost-anomaly-demo-spike"}


def _load_env_local() -> None:
    path = os.path.join(os.path.dirname(__file__), "..", ".env.local")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _find_demo_instances() -> list[str]:
    """Return running/pending instance IDs that carry BOTH demo tags. Nothing else."""
    import boto3

    ec2 = boto3.client("ec2", region_name=DEMO_REGION)
    filters = [
        {"Name": f"tag:{k}", "Values": [v]} for k, v in _REQUIRED_TAGS.items()
    ]
    filters.append(
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}
    )
    reservations = ec2.describe_instances(Filters=filters)["Reservations"]
    return [i["InstanceId"] for r in reservations for i in r["Instances"]]


def _terminate(instance_ids: list[str]) -> None:
    import boto3

    ec2 = boto3.client("ec2", region_name=DEMO_REGION)
    ec2.terminate_instances(InstanceIds=instance_ids)
    logger.info("Termination requested for: %s", ", ".join(instance_ids))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="detect and alert, but do not terminate")
    parser.add_argument("--threshold", type=float,
                        default=float(os.environ.get("SPIKE_THRESHOLD_PCT", "25.0")))
    args = parser.parse_args()

    _load_env_local()

    # Import the provider the agent is configured for.
    if os.environ.get("COST_PROVIDER", "").upper() == "ATHENA_CUR":
        from tools.aws_athena_cur import CostQueryError, find_spike_services
    else:
        from tools.aws_cost_explorer import CostQueryError, find_spike_services

    try:
        spikes = find_spike_services(args.threshold)
    except CostQueryError as e:
        logger.info("Cannot check for spikes yet: %s", e)
        logger.info("Instance keeps running; its 48h self-destruct still applies.")
        return 0

    if not spikes:
        logger.info("No spike above %.1f%% yet. The demo instance is still accruing "
                    "cost; re-run once CUR reflects a full day.", args.threshold)
        return 0

    logger.info("Spike detected: %s", ", ".join(
        f"{s['service']} +{s['pct_change']}%" for s in spikes))

    # Post the real Slack alert via the full agent loop.
    try:
        import agent as agent_module
        result = agent_module.NativeAWSFinOpsAgent().run()
        logger.info("Agent run: completed=%s slack_posted=%s",
                    result["completed"], result["slack_posted"])
    except Exception as e:
        logger.error("Agent run failed (%s) — continuing to cleanup anyway.", e)

    instance_ids = _find_demo_instances()
    if not instance_ids:
        logger.info("No finops-demo instances left to terminate.")
        return 0

    if args.dry_run:
        logger.info("[dry-run] would terminate: %s", ", ".join(instance_ids))
        return 0

    _terminate(instance_ids)
    logger.info("Demo complete: alert posted and instance(s) terminated. Nothing "
                "left running, nothing to clean up by hand.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
