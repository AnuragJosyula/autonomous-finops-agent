#!/usr/bin/env python3
"""
scripts/teardown.py — Remove everything the autonomous demo created.

Deletes, in order:
  1. the finops-demo EC2 instance (if still running)
  2. the CloudFormation stack finops-agent (Lambda, IAM role, EventBridge rule)
  3. the Athena table + database finops_cur.cur_projected
  4. the Lambda zip and Athena query results in S3

Leaves alone: the CUR export, its S3 bucket, and the CUR data itself.

Usage:
  python scripts/teardown.py            # show what would be deleted
  python scripts/teardown.py --yes      # actually delete
"""

import argparse
import sys
import time

import boto3

REGION = "us-east-1"
STACK = "finops-agent"
CUR_BUCKET = "aws-cur-reports-anurag"
GLUE_DB = "finops_cur"
GLUE_TABLE = "cur_projected"
DEMO_TAGS = {"Name": "finops-demo", "purpose": "cost-anomaly-demo-spike"}


def _demo_instances(ec2) -> list[str]:
    filters = [{"Name": f"tag:{k}", "Values": [v]} for k, v in DEMO_TAGS.items()]
    filters.append({"Name": "instance-state-name",
                    "Values": ["pending", "running", "stopping", "stopped"]})
    res = ec2.describe_instances(Filters=filters)["Reservations"]
    return [i["InstanceId"] for r in res for i in r["Instances"]]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yes", action="store_true", help="perform the deletions")
    args = ap.parse_args()

    ec2 = boto3.client("ec2", region_name=REGION)
    cf = boto3.client("cloudformation", region_name=REGION)
    athena = boto3.client("athena", region_name=REGION)
    s3 = boto3.client("s3")

    instances = _demo_instances(ec2)
    stacks = [s["StackName"] for s in cf.list_stacks(
        StackStatusFilter=["CREATE_COMPLETE", "UPDATE_COMPLETE", "ROLLBACK_COMPLETE"]
    )["StackSummaries"] if s["StackName"] == STACK]

    print("Will delete:")
    print(f"  EC2 instances : {instances or '(none)'}")
    print(f"  CFN stack     : {STACK if stacks else '(not found)'}")
    print(f"  Glue table    : {GLUE_DB}.{GLUE_TABLE}")
    print(f"  S3            : s3://{CUR_BUCKET}/lambda/  and  /athena-results/")
    print(f"  Keeping       : the CUR export, its bucket, and all CUR data")

    if not args.yes:
        print("\nDry run. Re-run with --yes to delete.")
        return 0

    if instances:
        ec2.terminate_instances(InstanceIds=instances)
        print(f"→ terminating {', '.join(instances)}")

    if stacks:
        cf.delete_stack(StackName=STACK)
        print(f"→ deleting stack {STACK} ...")
        try:
            cf.get_waiter("stack_delete_complete").wait(
                StackName=STACK, WaiterConfig={"Delay": 10, "MaxAttempts": 40})
            print("  stack deleted")
        except Exception as e:
            print(f"  stack delete wait ended: {e}")

    for q in (f"DROP TABLE IF EXISTS {GLUE_DB}.{GLUE_TABLE}",
              f"DROP DATABASE IF EXISTS {GLUE_DB}"):
        qid = athena.start_query_execution(
            QueryString=q, WorkGroup="primary",
            ResultConfiguration={"OutputLocation": f"s3://{CUR_BUCKET}/athena-results/"},
        )["QueryExecutionId"]
        while athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]["State"] \
                in ("QUEUED", "RUNNING"):
            time.sleep(1)
        print(f"→ {q}")

    for prefix in ("lambda/", "athena-results/"):
        keys = []
        for page in s3.get_paginator("list_objects_v2").paginate(
                Bucket=CUR_BUCKET, Prefix=prefix):
            keys += [{"Key": o["Key"]} for o in page.get("Contents", [])]
        for i in range(0, len(keys), 1000):
            s3.delete_objects(Bucket=CUR_BUCKET, Delete={"Objects": keys[i:i + 1000]})
        print(f"→ deleted {len(keys)} object(s) under s3://{CUR_BUCKET}/{prefix}")

    print("\nTeardown complete. Nothing left running.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
