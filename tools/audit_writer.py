"""
tools/audit_writer.py — Write a run audit record to Elasticsearch.

Every agent run writes one document to cost-anomaly-audit-YYYY.MM.dd,
regardless of whether anomalies were found or Slack delivered.
This provides a full audit trail for debugging, alerting on agent failures,
and tracking cost savings over time.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from tools.elastic_search import _get_es_client

logger = logging.getLogger(__name__)

AUDIT_INDEX_PREFIX = "cost-anomaly-audit"


def write_audit(
    run_id: str,
    anomalies_found: int,
    slack_delivered: bool,
    duration_seconds: float,
    token_count: int,
    error: str | None = None,
) -> dict[str, Any]:
    """
    Index one audit document for this agent run.

    Args:
        run_id:           UUID of this run.
        anomalies_found:  Number of cost anomalies detected.
        slack_delivered:   True if the Slack message was successfully posted.
        duration_seconds: Total wall-clock seconds for the run.
        token_count:      Total Bedrock tokens consumed (input + output).
        error:            Error message if something failed, else None.

    Returns:
        Dict with indexed=True/False, index name, doc_id, and any error.
    """
    now = datetime.now(timezone.utc)
    index_name = f"{AUDIT_INDEX_PREFIX}-{now.strftime('%Y.%m.%d')}"

    doc = {
        "@timestamp": now.isoformat(),
        "run_id": run_id,
        "anomalies_found": anomalies_found,
        "slack_delivered": slack_delivered,
        "duration_seconds": round(duration_seconds, 2),
        "token_count": token_count,
        "error": error,
        "status": "error" if error else "success",
    }

    try:
        es = _get_es_client()
        result = es.index(index=index_name, document=doc)
        doc_id = result.get("_id", "unknown")
        logger.info("Audit record written to %s (doc_id=%s)", index_name, doc_id)
        return {
            "indexed": True,
            "index": index_name,
            "doc_id": doc_id,
            "error": None,
        }
    except Exception as e:
        error_msg = f"Audit write failed: {e}"
        logger.error(error_msg)
        return {
            "indexed": False,
            "index": index_name,
            "doc_id": None,
            "error": error_msg,
        }
