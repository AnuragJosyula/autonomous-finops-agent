# Elastic Cost Analyzer — The "Aha!" End-to-End Conceptual Guide

> **Goal of this document:** Provide the complete, high-level conceptual picture of how every service, role, database, and AI loop connects together — so you intuitively understand how your FinOps agent works end-to-end.

---

## 💡 The "Aha!" Mental Model

Imagine running a company where **AWS bills** arrive daily, **developers deploy code** continuously, and **cost spikes** happen unexpectedly.

Instead of paying a human engineer $120k/year to check AWS bills every morning, cross-reference GitHub deploy logs, find out who pushed bad code, and post in Slack... **you built a 7-step autonomous AI system for ~$3/month.**

Here is the 10-second summary:
1. **EventBridge** is the **Alarm Clock** (wakes up Lambda every morning at 8:00 AM UTC).
2. **Lambda** is the **Serverless Worker** (hosts the agent code and handles network calls).
3. **IAM Roles** are the **Security Badges** (give Lambda and Elastic permission to access AWS services).
4. **Elasticsearch** is the **Single Source of Truth** (stores daily AWS costs, deployment logs, and agent audit trails).
5. **Amazon Bedrock (Claude)** is the **AI Brain** (reasons through the numbers, correlates spikes with deploys, and writes fixes).
6. **Slack Webhook** is the **Megaphone** (delivers actionable alerts directly to the `#finops` channel).

---

## 🗺️ The Architecture Map

```
                          ┌──────────────────────────┐
                          │   AWS EventBridge Cron   │
                          │   cron(0 8 * * ? *)      │
                          └─────────────┬────────────┘
                                        │ (Triggers daily at 8 AM)
                                        ▼
                          ┌──────────────────────────┐
                          │     AWS Lambda Host      │
                          │   (cost-anomaly-agent)   │
                          └──────┬────────────┬──────┘
                                 │            │
         ┌───────────────────────┘            └───────────────────────┐
         │ (Queries & Writes Data)                   │ (AI Reasoning & Tool-Calls)
         ▼                                           ▼
┌────────────────────────────────┐         ┌────────────────────────────────┐
│   Elastic Cloud Serverless     │         │   Amazon Bedrock (Claude AI)   │
│ ├── metrics-aws.billing-*      │         │ ├── System Prompt: 7 Steps     │
│ ├── deploy-events-*            │         │ └── Converse API (Tool Loop)   │
│ └── cost-anomaly-audit-*       │         └────────────────────────────────┘
└────────────────────────────────┘
         │
         │ (Ingests real AWS bills agentlessly)
         ▲
┌────────────────────────────────┐
│  AWS Cost Explorer API         │
│  (via IAM User:                │
│   elastic-billing-reader)      │
└────────────────────────────────┘

         │ (When spikes are found)
         ▼
┌────────────────────────────────┐
│  Slack Webhook (#finops)       │
│  Block Kit Card Alert          │
└────────────────────────────────┘
```

---

## 🔐 1. IAM Roles & Security Badges (Who Can Talk to Whom?)

Security in AWS follows the **Principle of Least Privilege**. Nothing can talk to anything else unless explicitly granted permission via IAM.

You created **two distinct IAM identities**:

### A. `elastic-billing-reader` (IAM User)
* **What it is:** Access keys (`AKIA...`) given to **Elastic Cloud**.
* **Why it exists:** Elastic Cloud is hosted outside your AWS account. To automatically pull your daily AWS Cost Explorer metrics into Elasticsearch without servers, Elastic uses this user.
* **Permissions granted:** 
  - `ce:GetCostAndUsage` (reads AWS Cost Explorer data)
  - `cloudwatch:GetMetricData` (reads CloudWatch metrics)

### B. `cost-anomaly-agent-lambda-role` (Lambda IAM Role)
* **What it is:** The security badge worn by your **AWS Lambda function**.
* **Why it exists:** When Lambda runs, it needs AWS permission to call Amazon Bedrock and write logs to CloudWatch.
* **Permissions attached:**
  - **`AWSLambdaBasicExecutionRole`**: Allows Lambda to write execution logs to AWS CloudWatch Logs.
  - **`BedrockInvokePolicy`** (`bedrock:InvokeModel`): Allows Lambda to talk to Bedrock's Claude Sonnet AI model.

---

## 📊 2. Elasticsearch & Elastic Cloud (The Single Source of Truth)

Elasticsearch acts as the centralized data store for three completely different types of information, organized into **Data Streams**:

```
Elasticsearch Cluster
├── 1. metrics-aws.billing-*    <-- Ingests daily AWS spend per service ($)
├── 2. deploy-events-*          <-- Ingests CI/CD deployments (Who, What, When)
└── 3. cost-anomaly-audit-*     <-- Agent audit logs (Run duration, token costs, errors)
```

### Why Data Streams?
In Elastic 8+, time-series metrics (like hourly billing data) are stored in **Data Streams** (e.g. `.ds-metrics-aws.billing-2026.08.25-000001`). Data Streams automatically partition old data, keep queries fast, and allow rollups over 7-day baselines.

1. **`metrics-aws.billing-*`**: Filled automatically by the Elastic AWS Integration (or seeded by `seed_billing.py`). Contains hourly cost entries per AWS service (`Amazon EC2`, `Amazon S3`, `Amazon RDS`).
2. **`deploy-events-*`**: Written by your CI/CD pipelines (GitHub Actions / Jenkins) whenever code is deployed. Contains `service`, `version` (e.g. `v2.3.1`), `deployed_by`, and timestamp.
3. **`cost-anomaly-audit-*`**: Written by the agent after every execution. Ensures you have an immutable record of every run, how many tokens were used, and whether Slack was alerted.

---

## ⏰ 3. AWS EventBridge (The Alarm Clock)

AWS Lambda doesn't run continuously — it sleeps to save money.

* **EventBridge Rule:** `cost-anomaly-agent-daily`
* **Schedule:** `cron(0 8 * * ? *)` (Every day at 8:00 AM UTC)
* **Role:** Every morning at 8 AM UTC, EventBridge sends a trigger signal to your Lambda function. You pay $0.00 for idle time.

---

## ⚡ 4. AWS Lambda (The Execution Host)

AWS Lambda is a **short-lived container host**. When EventBridge triggers it:

1. Lambda spins up a micro-container running Python 3.12.
2. Reads environment variables configured on the function:
   - `ES_URL` & `ES_API_KEY` (Elasticsearch connection)
   - `SLACK_WEBHOOK_URL` (Slack destination)
   - `BEDROCK_MODEL_ID` & `AWS_BEDROCK_REGION` (AI model selection)
3. Instantiates `CloudCostAnomalyAgent` and executes `agent.run()`.
4. Destroys the container once finished (~5-15 seconds total run time).

---

## 🧠 5. Amazon Bedrock & The Converse AI Loop (The Brain)

This is where the magic happens. Rather than writing thousands of hardcoded `if/else` conditions, **Claude AI acts as the reasoning engine**.

### How Bedrock Tool-Calling Works (The Converse API)

```
  Lambda (Python Host)                                   Amazon Bedrock (Claude)
          │                                                       │
          ├─────── 1. User Prompt + 5 Tool Schemas ──────────────►│
          │                                                       │ (Reads instructions:
          │                                                       │  "Follow 7-step sequence")
          │◄────── 2. "Call tool: find_spike_services(25%)" ──────┤
          │                                                       │
  (Executes ES query)                                             │
          ├─────── 3. Return Tool Result: [EC2 +43.1% Spike] ────►│
          │                                                       │
          │◄────── 4. "Call tool: get_cost_timeseries(EC2)" ──────┤
          │                                                       │
  (Executes ES query)                                             │
          ├─────── 5. Return Timeseries: [Spike started 17:00] ──►│
          │                                                       │
          │◄────── 6. "Call tool: find_deploys(17:00 +-12h)" ─────┤
          │                                                       │
  (Executes ES query)                                             │
          ├─────── 7. Return Deploy: [checkout v2.3.1 at 14:00] ──►│
          │                                                       │
          │                                                       │ (Synthesizes root cause &
          │                                                       │  suggested fix)
          │                                                       │
          │◄────── 8. "Call tool: post_slack_alert(...)" ─────────┤
          │                                                       │
  (Posts to Slack)                                                │
          ├─────── 9. Return Slack Delivery Success ──────────────►│
          │                                                       │
          │◄────── 10. "Call tool: write_audit(...)" ─────────────┤
          │                                                       │
  (Writes Audit Doc)                                              │
          ├─────── 11. Return Audit Success ──────────────────────►│
          │                                                       │
          │◄────── 12. "end_turn" (Done!) ────────────────────────┤
```

### Key Takeaways on Bedrock:
* **The System Prompt Enforces Logic:** The system prompt instructs Claude to strictly follow Steps 1 through 7 and enforces rules like *"Never fabricate numbers"* and *"If no deploy found, explicitly state 'No deploy found in ±12h window'"*.
* **Claude Controls the Flow:** Claude chooses which tool to run next based on the outputs of previous tools.
* **Python Executes the Actions:** Claude never connects to Elasticsearch directly. Claude simply outputs JSON requesting tool execution, and Python executes the query on Claude's behalf.

---

## 📢 6. Slack Webhook (The Receiver)

When Claude decides to call `post_slack_alert()`, Python builds a **Slack Block Kit** JSON payload.

- **Payload format:** Contains rich visual elements (headers, formatted tables, section dividers, bold text).
- **Transport:** HTTP `POST` request to `https://hooks.slack.com/services/...`
- **Result:** The `#finops` channel receives a clean, readable card detailing the service, dollar delta, root cause, and estimated cost saving.

---

## 🔄 Summary of the Daily Cycle (The Full "Aha!" Picture)

```
 8:00 AM UTC  ──► EventBridge fires trigger
      │
      ▼
 8:00:01 AM   ──► Lambda container starts & initializes Bedrock + Elastic clients
      │
      ▼
 8:00:03 AM   ──► Claude compares today's cost vs 7-day average in Elasticsearch
      │
      ├─────► NO Anomalies? ──► Writes Audit Doc ──► Stops (Slack remains silent)
      │
      └─────► Anomalies Found?
                  │
 8:00:06 AM       ├──► Claude pinpoints exact spike hour (e.g. 17:00 UTC)
 8:00:08 AM       ├──► Claude checks deploy logs near 17:00 UTC
 8:00:10 AM       ├──► Claude reasons root cause & computes estimated dollar savings
 8:00:12 AM       ├──► Claude formats & posts Slack alert to #finops
 8:00:14 AM       └──► Claude writes audit log to cost-anomaly-audit-*
      │
      ▼
 8:00:15 AM   ──► Lambda container turns off. Total cost of execution: < $0.001
```
