# AWS Native FinOps Agent — Lambda Deployment Guide

Deploy the cost anomaly agent as a Lambda function triggered daily by EventBridge.

---

## Architecture

```text
EventBridge (cron 8 AM UTC)
  └─► Lambda (agent.py)
        ├─► Cost Explorer API ──► baseline vs today's spend
        ├─► CloudTrail API ─────► infrastructure changes near spike
        ├─► Bedrock (Claude) ──► root cause reasoning
        └─► Slack webhook ─────► alert with fix + $ saving
```

---

## Deployment (4 steps)

### 1. Create IAM Role

```bash
aws iam create-role \
  --role-name finops-agent-role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]
  }'

aws iam put-role-policy \
  --role-name finops-agent-role \
  --policy-name FinOpsAgentPolicy \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": [
        "ce:GetCostAndUsage",
        "cloudtrail:LookupEvents",
        "bedrock:InvokeModel",
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "*"
    }]
  }'
```

> Add Athena/Glue/S3 permissions only if using `COST_PROVIDER=ATHENA_CUR`.

### 2. Package

```bash
cd aws_native_plug_and_play
zip -r agent.zip agent.py tools/
```

### 3. Create Lambda

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

aws lambda create-function \
  --function-name finops-cost-anomaly-agent \
  --runtime python3.12 \
  --role arn:aws:iam::${ACCOUNT_ID}:role/finops-agent-role \
  --handler agent.lambda_handler \
  --zip-file fileb://agent.zip \
  --timeout 60 \
  --environment "Variables={
    SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL,
    BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-6,
    SPIKE_THRESHOLD_PCT=25.0
  }"
```

### 4. Schedule with EventBridge

```bash
aws events put-rule \
  --name finops-agent-daily \
  --schedule-expression "cron(0 8 * * ? *)"

aws lambda add-permission \
  --function-name finops-cost-anomaly-agent \
  --statement-id DailyTrigger \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:us-east-1:${ACCOUNT_ID}:rule/finops-agent-daily

aws events put-targets \
  --rule finops-agent-daily \
  --targets "Id"="1","Arn"="arn:aws:lambda:us-east-1:${ACCOUNT_ID}:function:finops-cost-anomaly-agent"
```

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `COST_PROVIDER` | `COST_EXPLORER` | `COST_EXPLORER` or `ATHENA_CUR` |
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-sonnet-4-6` | Bedrock inference profile |
| `SPIKE_THRESHOLD_PCT` | `25.0` | % above baseline to flag |
| `SLACK_WEBHOOK_URL` | *(required)* | Slack webhook URL |
| `AWS_BEDROCK_REGION` | `us-east-1` | Bedrock region |
| `FINOPS_AWS_REGION` | `us-east-1` | Cost Explorer / CloudTrail / Athena region |
| `AGENT_MAX_ITERATIONS` | `8` | Max Bedrock loop iterations |

> `FINOPS_AWS_REGION` exists because `AWS_REGION` is a **reserved** Lambda environment variable and cannot be set on a function. It falls back to `AWS_REGION`, then `us-east-1`.


### Athena CUR mode (enterprise)

Set `COST_PROVIDER=ATHENA_CUR` and add:

| Variable | Default | Description |
|---|---|---|
| `ATHENA_DATABASE` | `athenacurcfn_aws_c_u_r` | Glue database name |
| `ATHENA_TABLE` | `aws_cur` | CUR table name |
| `ATHENA_OUTPUT_LOCATION` | *(workgroup default)* | Athena results bucket |

---

## Cost Explorer vs Athena CUR

| | Cost Explorer | Athena CUR |
|---|---|---|
| **Accounts** | 1 account | 50+ (Organizations) |
| **Cost/query** | $0.01 per API call | ~$0.005 per GB scanned |
| **Detail** | Service-level totals | Resource ARNs, FOCUS 1.4 schema |
| **Setup** | None | CUR report + Glue Crawler (one-time) |
