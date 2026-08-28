# Cloud Cost Anomaly Agent 💰🤖

An autonomous **FinOps AI Agent** that detects AWS cost spikes, correlates them with recent code deployments, and posts root-cause alerts with actionable fix recommendations to Slack — automatically every morning.

Built with **Amazon Bedrock (Claude)**, **Elastic Cloud Serverless**, and **AWS Lambda**. Operates serverlessly for **~$3–5/month**.

---

## ❓ What Is This?

This project is an **agentic cloud cost monitoring system**. Instead of engineers manually logging into AWS Cost Explorer and searching through GitHub deployment logs when a bill spikes, this agent runs automatically every morning, reasons through billing & deployment data using AI, and posts an actionable alert to Slack.

---

## 💡 Why This Was Done & How It Helps

### The Problem

* AWS bills arrive daily, but cost spikes (e.g. Kubernetes pod autoscaling, orphaned NAT gateways, unoptimized queries) often go unnoticed for days or weeks.
* Correlating a cost spike with code deployments requires checking multiple tools (AWS Cost Explorer, Datadog, GitHub Actions, Kubernetes HPA metrics).

### How It Helps

* **Zero Manual Triage:** The agent runs on a daily schedule, calculates a 7-day baseline, and flags any service exceeding its baseline by $\ge 25\%$.
* **Automated Root-Cause Correlation:** Matches the exact hour of the cost spike with recent CI/CD code deployments ($\pm 12\text{h}$ window).
* **Actionable Dollar Savings:** Provides specific engineering fix instructions along with estimated daily dollar savings.
* **Hands-on Observability:** Originally built with Elastic Cloud Serverless (ELK) to get hands-on experience with unified telemetry & audit data streams, with a 100% AWS-Native Plug & Play alternative included for zero-database setups.
* **Low Cost:** Completely serverless — costs ~$3–5/month to run.

---

## 📊 How It Gives Results (Sample Output)

When an anomaly is detected, the agent generates and posts a structured **Slack Block Kit card** to `#finops`:

```text
🔴 AWS Cost Anomaly Detected
Run `manual-demo-run` · 1 anomaly(ies) found · 4.2s

─────────────────────────────────────────────────────────────
*Amazon EC2* · `checkout-team`
Today: *$847.20* (+43.1% vs 7-day avg)
Baseline: $592.10/day · Delta: *+$255.10*

🔍 Root Cause:
HPA scaled checkout pods 3->12 replicas 6h after deploy v2.3.1 at 14:00 UTC -- CPU utilisation held at 18%, minReplicas set too high.

💡 Suggested Fix:
Reduce minReplicas to 3 in checkout-service/k8s/hpa.yaml. Estimated saving if fixed today: ~$220.
```

---

## 🔌 Plug & Play Mode: 100% AWS Native (No Database Required)

In addition to the Elasticsearch unified telemetry mode, this repository includes a **100% AWS-Native Plug & Play** version inside the [`aws_native_plug_and_play/`](./aws_native_plug_and_play/) directory!

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                    AWS Native Plug & Play Architecture                  │
│                                                                         │
│  AWS Cost Explorer API  ──► boto3.client('ce')       ──► Baseline & Spike│
│  AWS CloudTrail         ──► boto3.client('cloudtrail')──► Deploy Events  │
│  Amazon Bedrock         ──► Bedrock Converse API     ──► AI Reasoning   │
│  Slack Webhook          ──► HTTP POST                ──► Alert Card     │
└─────────────────────────────────────────────────────────────────────────┘
```

### Why use AWS Native Plug & Play?

* **Zero External Databases:** No Elasticsearch cluster, no API keys, no index setup.
* **Pure AWS Credentials:** Queries `ce:GetCostAndUsage` and `cloudtrail:LookupEvents` directly via standard `boto3` IAM permissions.
* **Instant Deployment:** See [`aws_native_plug_and_play/README.md`](./aws_native_plug_and_play/README.md) for 1-click AWS CLI / CloudFormation deployment steps to monitor any AWS account immediately!

---

## 🛠️ Key Technical Architecture

```text
EventBridge (cron 0 8 * * ? *)
  └──► AWS Lambda (Thin Orchestrator)
        ├──► Amazon Bedrock (Claude Converse API - 7-Step Tool Loop)
        │     ├── 1. find_spike_services    (Compare today vs 7-day baseline)
        │     ├── 2. get_cost_timeseries    (Pinpoint exact spike hour)
        │     ├── 3. find_deploys_near_spike(Match with CI/CD deploy events)
        │     ├── 4. post_slack_alert       (Send Block Kit alert card)
        │     └── 5. write_audit            (Log run telemetry to Elasticsearch)
        │
        ├──► Elasticsearch
        │     ├── metrics-aws.billing-*    (AWS daily spend per service)
        │     ├── deploy-events-*          (CI/CD code deployment logs)
        │     └── cost-anomaly-audit-*     (Agent run audit logs)
        │
        └──► Slack (#finops)
```

---

## 🚨 Issues Faced & How We Fixed Them

During the development and setup of this project, we encountered and resolved several real-world engineering challenges:

### 1. PyPI / Pip Installation Network Connection Failures

* **Issue:** Running `pip install` failed with connection retries (`[Errno 11001] getaddrinfo failed`) due to a global NVIDIA PyPI index (`pypi.ngc.nvidia.com`) lingering in system `pip.ini` files.
* **Fix:** Cleaned global `pip.ini` configuration files under `C:\ProgramData\pip\pip.ini` and user profiles, enforcing direct PyPI resolution (`https://pypi.org/simple`).

### 2. AWS Bedrock Model Identifier & Access Permissions

* **Issue:** Initial test invocations returned `ResourceNotFoundException` (deprecated model string) and `AccessDeniedException` (missing AWS Marketplace subscription for Anthropic models).
* **Fix:** Updated the model identifier to an active inference profile (`us.anthropic.claude-3-5-sonnet-20241022-v2:0` / `us.anthropic.claude-sonnet-4-5-20250929-v1:0`) and completed the AWS Bedrock Anthropic model access submission form.

### 3. PowerShell CLI JSON Payload Escaping

* **Issue:** Invoking AWS CLI commands with inline JSON strings (`--payload '{"source":"manual-test"}'`) in PowerShell caused quote parsing errors (`Could not parse payload into json: Unexpected character`).
* **Fix:** Switched to file-based JSON arguments (`file://policy.json`, `file://payload.json`) or native Python `boto3` script invocations.

### 4. Elasticsearch API Key Privilege Scoping

* **Issue:** The agent needs read permissions on billing indices and write permissions on audit indices, but seeding required administrative rights.
* **Fix:** Separated credentials into two roles:
  * **Superuser Key:** Used exclusively for setup & seeding (`scripts/seed_billing.py`).
  * **Restricted Key:** Used by the agent (`agent.py`) with strict minimal privileges (`read` on `metrics-aws.billing-*`/`deploy-events-*`, `index` on `cost-anomaly-audit-*`).

---

## 📁 Repository Layout

```text
.
├── agent.py                 # AWS Lambda handler & Bedrock Converse API loop
├── aws_native_plug_and_play/# 100% AWS Native Plug & Play mode (Zero external DB)
│   ├── agent.py             # Native Lambda orchestrator
│   ├── README.md            # Plug & Play setup & 1-click deployment guide
│   └── tools/               # AWS Cost Explorer & CloudTrail boto3 tools
├── tools/
│   ├── elastic_search.py    # Elasticsearch query functions (billing + deploy lookups)
│   ├── slack_notify.py      # Slack Block Kit alert builder
│   └── audit_writer.py      # Audit trail writer for Elasticsearch
├── scripts/
│   └── seed_billing.py      # Demo data seeder (7-day baseline + cost spike + deploy event)
├── tests/
│   └── test_integration.py  # 8 integration tests (fully mocked, no real cloud calls needed)
├── architecture_guide.md    # End-to-end conceptual architecture guide
├── requirements.txt         # Pinned Python dependencies
├── .env.example             # Environment variable template
└── .gitignore               # Security exclusions
```

---

## 🚀 Local Setup & Testing

### 1. Clone & Install Dependencies

```bash
git clone https://github.com/YOUR_USERNAME/elastic-cost-analyzer.git
cd elastic-cost-analyzer

python -m venv .venv
# On Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# On Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Run Integration Tests

```bash
python -m unittest tests.test_integration -v
```

*Expected: 8/8 tests pass in ~0.01s (all cloud calls mocked).*

---

## 📊 Deployment & Architecture Comparison Matrix

| Feature / Dimension | Mode 1: Elasticsearch Telemetry | Mode 2: AWS Native (Cost Explorer API) | Mode 3: Enterprise AWS Native (Athena CUR / FOCUS) |
|---|---|---|---|
| **Target Scale** | Full Observability Stack | Single-Account / Developer Sandbox | Enterprise Multi-Account (AWS Organizations) |
| **Billing Data Source** | Elasticsearch (`metrics-aws.billing-*`) | AWS Cost Explorer (`ce:GetCostAndUsage`) | AWS CUR Export via S3 + Athena SQL |
| **Data Standard** | Elastic Common Schema (ECS) | AWS CE Proprietary | **FOCUS 1.4 Standardized Schema** |
| **Data Granularity** | Index-level daily/hourly spend | Service-level daily/hourly totals | Line-item Resource ARNs, Usage Types, Pricing Models |
| **Monthly Execution Cost** | ~$3–$5 / month (Lambda + Elastic) | ~$1–$3 / month ($0.01 per CE API call) | **~$5–$8 / month** (Parquet S3 scanning ~$0.30 + Bedrock) |
| **External Dependencies** | Elastic Cloud Serverless Cluster | **Zero** (Pure AWS Lambda + boto3) | **Zero** (Pure AWS Lambda + S3 + Athena) |
| **Setup Complexity** | API key & index mapping setup | **1-Click AWS CLI / SAM deploy** | 1-Time CUR S3 Export & Glue Crawler |
| **Primary Code File** | [`agent.py`](./agent.py) | [`aws_native_plug_and_play/agent.py`](./aws_native_plug_and_play/agent.py) | [`aws_native_plug_and_play/tools/aws_athena_cur.py`](./aws_native_plug_and_play/tools/aws_athena_cur.py) |

---

## 📄 License

MIT License. Free to use and modify.
