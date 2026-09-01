# Cloud Cost Anomaly Agent 💰🤖

An AI agent that detects AWS cost spikes every morning, checks what infrastructure changed, and sends a Slack alert with root cause and fix instructions.

---

## How It Works

```text
EventBridge (daily 8 AM UTC)
  └─► Lambda
        ├─► Cost Explorer API ──► "EC2 is +43% above baseline"
        ├─► CloudTrail API ─────► "RunInstances called 6h before spike"
        ├─► Bedrock (Claude) ──► "HPA scaled to 12 replicas, minReplicas too high"
        └─► Slack webhook ─────► Alert card with fix + $ saving
```

---

## Two Modes

| | Cost Explorer (default) | Athena CUR |
|---|---|---|
| **Best for** | Single account, zero setup | 50+ accounts, AWS Organizations |
| **Cost** | ~$0.04/run (~$1.20/month daily) | ~$0.05/run + Glue/S3 |
| **Detail** | Service-level totals | Resource ARNs, pricing models |
| **Setup** | 2 minutes | CUR + Glue Crawler (one-time) |
| **Code** | [`aws_native_plug_and_play/`](./aws_native_plug_and_play/) | Same dir, set `COST_PROVIDER=ATHENA_CUR` |

---

## Sample Slack Alert

```text
🔴 AWS Cost Anomaly Detected
Run abc123 · 1 anomaly · 4.2s

───────────────────────────────────────
Amazon EC2 · AWS Account
Today: $847.20 (+43.1% vs 7-day avg)
Baseline: $592.10/day · Delta: +$255.10

🔍 Root Cause:
HPA scaled checkout pods 3→12 replicas after deploy v2.3.1.
CPU stayed at 18% — minReplicas set too high.

💡 Fix:
Reduce minReplicas to 3 in hpa.yaml. Saving: ~$220/day.
```

---

## Quick Start (2 minutes)

### 1. Clone & configure

```bash
git clone https://github.com/AnuragJosyula/autonomous-finops-agent.git
cd autonomous-finops-agent
```

### 2. Run locally

```bash
cd aws_native_plug_and_play
python -c "
import logging; logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
from agent import NativeAWSFinOpsAgent; print(NativeAWSFinOpsAgent().run())
"
```

Requires AWS credentials with `ce:GetCostAndUsage`, `cloudtrail:LookupEvents`, and `bedrock:InvokeModel`.

### 3. Deploy to Lambda

See [`aws_native_plug_and_play/README.md`](./aws_native_plug_and_play/README.md) for the full 4-step Lambda + EventBridge deployment.

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `COST_PROVIDER` | `COST_EXPLORER` | `COST_EXPLORER` or `ATHENA_CUR` |
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-sonnet-4-6` | Bedrock inference profile ID |
| `SPIKE_THRESHOLD_PCT` | `25.0` | % above baseline to trigger alert |
| `SLACK_WEBHOOK_URL` | *(required)* | Slack incoming webhook |
| `AWS_BEDROCK_REGION` | `us-east-1` | Bedrock region |
| `AWS_REGION` | `us-east-1` | Region for Cost Explorer, CloudTrail, Athena |
| `AGENT_MAX_ITERATIONS` | `5` | Max Bedrock loop iterations (cost guard) |
| `ATHENA_DATABASE` | `athenacurcfn_aws_cur` | Athena DB (only for `ATHENA_CUR`) |
| `ATHENA_TABLE` | `aws_cur` | Athena table (only for `ATHENA_CUR`) |
| `ATHENA_OUTPUT_LOCATION` | `s3://aws-athena-query-results-finops/` | Athena results S3 path |

---

## Project Structure

```text
├── aws_native_plug_and_play/     # ← Main agent (use this)
│   ├── agent.py                  # Bedrock Converse loop
│   ├── requirements.txt          # Python deps
│   ├── README.md                 # Lambda deployment guide
│   └── tools/
│       ├── aws_cost_explorer.py  # Cost Explorer API queries
│       ├── aws_athena_cur.py     # Athena CUR/FOCUS queries
│       ├── aws_cloudtrail.py     # CloudTrail event lookup
│       └── slack_notify.py       # Slack Block Kit alerts
├── scripts/
│   ├── validate_athena.py        # Athena setup validator
│   └── seed_billing.py           # Demo data generator
├── agent.py                      # Legacy Elasticsearch mode
├── tools/                        # Legacy ES tools
└── tests/                        # Unit tests
```

---

## Cost Estimate

| Component | Per run | Monthly (1x/day) |
|---|---|---|
| Bedrock (Sonnet 4.6) | ~$0.02 | ~$0.60 |
| Cost Explorer API | ~$0.02 | ~$0.60 |
| CloudTrail | Free | Free |
| Lambda | ~$0.00 | ~$0.00 |
| **Total** | **~$0.04** | **~$1.20** |

---

## License

MIT
