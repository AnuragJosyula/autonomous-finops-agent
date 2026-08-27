# 🔌 AWS Native Plug & Play FinOps Agent (No Database Required)

A **100% AWS-Native, Zero-Database** version of the FinOps AI Cost Anomaly Agent.

It queries **AWS Cost Explorer** and **AWS CloudTrail** directly via `boto3` SDK — requiring **zero third-party databases**, zero API keys, and zero external cluster setup!

---

## 🗺️ Architecture Overview

```text
                      ┌──────────────────────────┐
                      │   AWS EventBridge Cron   │
                      │   cron(0 8 * * ? *)      │
                      └─────────────┬────────────┘
                                    │ (Triggers daily at 8 AM)
                                    ▼
                      ┌──────────────────────────┐
                      │     AWS Lambda Host      │
                      │  (aws_native_plug_and_play)
                      └──────┬────────────┬──────┘
                             │            │
     ┌───────────────────────┘            └───────────────────────┐
     │ (boto3: ce:GetCostAndUsage)               │ (boto3: cloudtrail:LookupEvents)
     ▼                                           ▼
┌────────────────────────────────┐         ┌────────────────────────────────┐
│     AWS Cost Explorer API      │         │       AWS CloudTrail API       │
│  - 7-Day Baseline Spend ($)    │         │  - Infrastructure Changes      │
│  - Today's Hourly Spikes ($)   │         │  - EC2/RDS/EKS Modify Calls    │
└────────────────────────────────┘         └────────────────────────────────┘
     │                                           │
     └───────────────────────┬───────────────────┘
                             │
                             ▼
              ┌──────────────────────────────┐
              │  Amazon Bedrock (Claude AI)  │
              │  - Synthesizes Root Cause    │
              │  - Calculates Dollar Savings │
              └──────────────┬───────────────┘
                             │
                             ▼
              ┌──────────────────────────────┐
              │    Slack Webhook (#finops)   │
              │    Block Kit Alert Card      │
              └──────────────────────────────┘
```

---

## 🌟 Key Features

* ⚡ **Zero Infrastructure Setup:** No Elasticsearch, no vector DB, no external serverless subscriptions.
* 🔐 **Pure AWS IAM Security:** Uses AWS IAM roles directly (`ce:GetCostAndUsage`, `cloudtrail:LookupEvents`, `bedrock:InvokeModel`).
* 💰 **Ultra Low Cost:** Only pays for Lambda execution (~10s/day) and Bedrock tokens. Total cost: **~$1–3/month**.
* 🚀 **1-Click Deployment:** Can be packaged into AWS Lambda directly via AWS CLI, SAM, or Terraform.

---

## 🚀 Quick Deployment Guide

### Step 1: Create IAM Role & Attach Policies

```bash
# Create IAM Role
aws iam create-role \
  --role-name native-finops-agent-role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]
  }'

# Attach Permissions Policy
aws iam put-role-policy \
  --role-name native-finops-agent-role \
  --policy-name NativeFinOpsPermissions \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": [
          "ce:GetCostAndUsage",
          "ce:GetDimensionValues",
          "cloudtrail:LookupEvents",
          "bedrock:InvokeModel",
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ],
        "Resource": "*"
      }
    ]
  }'
```

### Step 2: Package Code

```bash
cd aws_native_plug_and_play
zip -r native-cost-agent.zip agent.py tools/
```

### Step 3: Deploy Lambda Function

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

aws lambda create-function \
  --function-name native-cost-anomaly-agent \
  --runtime python3.12 \
  --role arn:aws:iam::${ACCOUNT_ID}:role/native-finops-agent-role \
  --handler agent.lambda_handler \
  --zip-file fileb://native-cost-agent.zip \
  --timeout 60 \
  --environment "Variables={SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL,AWS_BEDROCK_REGION=us-east-1,BEDROCK_MODEL_ID=us.anthropic.claude-3-5-sonnet-20241022-v2:0,SPIKE_THRESHOLD_PCT=25.0}"
```

### Step 4: Add EventBridge Schedule (Daily at 8 AM UTC)

```bash
aws events put-rule --name native-cost-agent-daily --schedule-expression "cron(0 8 * * ? *)"

aws lambda add-permission \
  --function-name native-cost-anomaly-agent \
  --statement-id DailyTrigger \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:us-east-1:${ACCOUNT_ID}:rule/native-cost-agent-daily

aws events put-targets \
  --rule native-cost-agent-daily \
  --targets "Id"="1","Arn"="arn:aws:lambda:us-east-1:${ACCOUNT_ID}:function:native-cost-anomaly-agent"
```

---

## 🔧 Customization Options

| Environment Variable | Default Value | Description |
|---|---|---|
| `COST_PROVIDER` | `COST_EXPLORER` | Billing backend provider (`COST_EXPLORER` for Cost Explorer API or `ATHENA_CUR` for Enterprise Athena CUR/FOCUS) |
| `ATHENA_DATABASE` | `athenacurcfn_aws_cur` | Glue/Athena database name when `COST_PROVIDER=ATHENA_CUR` |
| `ATHENA_TABLE` | `aws_cur` | Athena CUR table name (supports FOCUS standardized columns) |
| `ATHENA_OUTPUT_LOCATION` | `s3://aws-athena-query-results-finops/` | S3 bucket for Athena query output staging |
| `SLACK_WEBHOOK_URL` | *(Required)* | Slack incoming webhook URL |
| `SPIKE_THRESHOLD_PCT` | `25.0` | Percentage increase over 7-day average required to trigger alert |
| `AWS_BEDROCK_REGION` | `us-east-1` | AWS region where Amazon Bedrock model is enabled |
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-3-5-sonnet-20241022-v2:0` | Amazon Bedrock model ID |

---

## 🏢 Enterprise Scaling: Cost Explorer vs Athena CUR (FOCUS)

| Dimension | Cost Explorer API (`COST_EXPLORER`) | Enterprise Athena CUR (`ATHENA_CUR`) |
|---|---|---|
| **Best For** | Single-account, quick zero-setup deployment | Multi-account / AWS Organizations, large scale |
| **Pricing** | $0.01 per API call | ~$0.005 per GB scanned (Athena S3 query) |
| **Granularity** | Service-level daily/hourly totals | Line-item resource ARNs, usage types, pricing models |
| **Schema** | Proprietary AWS CE format | Standardized FOCUS schema compatible |

