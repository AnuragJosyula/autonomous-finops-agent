# 🔌 AWS Native Plug & Play FinOps Agent (No Database Required)

A **100% AWS-Native, Zero-Database** version of the FinOps AI Cost Anomaly Agent.

It reads **AWS Cost Explorer** and **AWS CloudTrail** directly using the `boto3` Python SDK — requiring **no external databases**, no API keys, and no extra server setup!

---

## 🗺️ How It Works

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
│  - 7-Day Average Spend ($)     │         │  - Infrastructure Changes      │
│  - Today's Hourly Spikes ($)   │         │  - EC2/RDS/EKS Modify Calls    │
└────────────────────────────────┘         └────────────────────────────────┘
     │                                           │
     └───────────────────────┬───────────────────┘
                             │
                             ▼
              ┌──────────────────────────────┐
              │  Amazon Bedrock (Claude AI)  │
              │  - Finds Root Cause          │
              │  - Calculates Dollar Savings │
              └──────────────┬───────────────┘
                             │
                             ▼
              ┌──────────────────────────────┐
              │    Slack Webhook (#finops)   │
              │    Alert Card Message        │
              └──────────────────────────────┘
```

---

## 🌟 Key Features

* ⚡ **Zero Database Setup:** No Elasticsearch, no vector DB, no external serverless subscriptions.
* 🔐 **Pure AWS Security:** Uses standard AWS IAM roles directly (`ce:GetCostAndUsage`, `cloudtrail:LookupEvents`, `bedrock:InvokeModel`).
* 💰 **Ultra Low Cost:** Only pays for Lambda execution (10 seconds a day) and Bedrock tokens. Total cost: **About $1 to $3 per month**.
* 🚀 **Quick Deployment:** Can be packaged into AWS Lambda in 2 minutes using simple AWS CLI commands.

---

## 🚀 Quick 4-Step Deployment Guide

### Step 1: Create IAM Role & Attach Permissions

```bash
# Create IAM Role for Lambda
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
          "athena:StartQueryExecution",
          "athena:GetQueryExecution",
          "athena:GetQueryResults",
          "glue:GetTable",
          "glue:GetPartitions",
          "s3:GetObject",
          "s3:ListBucket",
          "s3:PutObject",
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

### Step 2: Zip the Code

```bash
cd aws_native_plug_and_play
zip -r native-cost-agent.zip agent.py tools/
```

### Step 3: Create the AWS Lambda Function

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

aws lambda create-function \
  --function-name native-cost-anomaly-agent \
  --runtime python3.12 \
  --role arn:aws:iam::${ACCOUNT_ID}:role/native-finops-agent-role \
  --handler agent.lambda_handler \
  --zip-file fileb://native-cost-agent.zip \
  --timeout 60 \
  --environment "Variables={SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL,AWS_BEDROCK_REGION=us-east-1,BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-5-20250929-v1:0,SPIKE_THRESHOLD_PCT=25.0}"
```

### Step 4: Add EventBridge Schedule (Triggers Daily at 8 AM UTC)

```bash
# Create Daily Schedule Rule
aws events put-rule --name native-cost-agent-daily --schedule-expression "cron(0 8 * * ? *)"

# Grant Permission to EventBridge to invoke Lambda
aws lambda add-permission \
  --function-name native-cost-anomaly-agent \
  --statement-id DailyTrigger \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:us-east-1:${ACCOUNT_ID}:rule/native-cost-agent-daily

# Attach Target to Rule
aws events put-targets \
  --rule native-cost-agent-daily \
  --targets "Id"="1","Arn"="arn:aws:lambda:us-east-1:${ACCOUNT_ID}:function:native-cost-anomaly-agent"
```

---

## 🔧 Simple Customization Options

| Environment Variable | Default Value | What It Does |
|---|---|---|
| `COST_PROVIDER` | `COST_EXPLORER` | Select cost mode (`COST_EXPLORER` for 1 account or `ATHENA_CUR` for 50+ accounts) |
| `ATHENA_DATABASE` | `athenacurcfn_aws_cur` | Athena database name (only used if `COST_PROVIDER=ATHENA_CUR`) |
| `ATHENA_TABLE` | `aws_cur` | Athena table name (supports FOCUS billing schema) |
| `ATHENA_OUTPUT_LOCATION` | `s3://aws-athena-query-results-finops/` | S3 folder for Athena query results |
| `SLACK_WEBHOOK_URL` | *(Required)* | Your Slack incoming webhook URL |
| `SPIKE_THRESHOLD_PCT` | `25.0` | Cost increase percentage needed to trigger an alert |
| `AWS_BEDROCK_REGION` | `us-east-1` | AWS region for Amazon Bedrock model |
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | Amazon Bedrock AI model ID |

---

## 🏢 Choosing Between Cost Explorer vs Athena CUR (FOCUS)

| Feature | Cost Explorer API (`COST_EXPLORER`) | Enterprise Athena CUR (`ATHENA_CUR`) |
|---|---|---|
| **Best For?** | Testing on 1 AWS account with zero setup | Large companies with 50+ AWS accounts |
| **Cost?** | $0.01 per API call | About $0.005 per GB scanned (S3 query) |
| **Detail Level?** | Service-level total costs | Resource-level details & billing models |
| **Format?** | Standard AWS Cost Explorer format | Standard FOCUS 1.4 schema compatible |
