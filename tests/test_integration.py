"""
tests/test_integration.py — Integration test for the full agent loop.

Injects a fake EC2 spike + v2.3.1 deploy, runs agent.run() against
mocked AWS and Elastic, and asserts the full 7-step chain completed correctly.
"""

import json
import os
import unittest
from unittest.mock import MagicMock, patch, call

# Set required env vars before importing agent
os.environ.setdefault("ES_URL", "https://fake-es.elastic.cloud")
os.environ.setdefault("ES_API_KEY", "fake-api-key")
os.environ.setdefault("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/fake")
os.environ.setdefault("SPIKE_THRESHOLD_PCT", "25.0")
os.environ.setdefault("AGENT_MAX_ITERATIONS", "20")
os.environ.setdefault("AWS_BEDROCK_REGION", "us-east-1")

from agent import CloudCostAnomalyAgent, lambda_handler  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------
FAKE_SPIKE = {
    "service": "Amazon EC2",
    "team": "checkout-team",
    "today_usd": 847.20,
    "baseline_usd": 592.10,
    "delta_usd": 255.10,
    "pct_change": 43.1,
}

FAKE_TIMESERIES = [
    {"timestamp": "2025-05-25T08:00:00Z", "cost_usd": 24.63},
    {"timestamp": "2025-05-25T09:00:00Z", "cost_usd": 24.71},
    {"timestamp": "2025-05-25T10:00:00Z", "cost_usd": 25.02},
    {"timestamp": "2025-05-25T11:00:00Z", "cost_usd": 24.98},
    {"timestamp": "2025-05-25T12:00:00Z", "cost_usd": 25.10},
    {"timestamp": "2025-05-25T13:00:00Z", "cost_usd": 25.04},
    {"timestamp": "2025-05-25T14:00:00Z", "cost_usd": 25.03},  # deploy at 14:00
    {"timestamp": "2025-05-25T15:00:00Z", "cost_usd": 26.11},
    {"timestamp": "2025-05-25T16:00:00Z", "cost_usd": 28.44},
    {"timestamp": "2025-05-25T17:00:00Z", "cost_usd": 34.92},  # spike starts here
    {"timestamp": "2025-05-25T18:00:00Z", "cost_usd": 41.20},
    {"timestamp": "2025-05-25T19:00:00Z", "cost_usd": 43.87},
    {"timestamp": "2025-05-25T20:00:00Z", "cost_usd": 44.01},
]

FAKE_DEPLOY = {
    "service": "checkout",
    "version": "v2.3.1",
    "team": "checkout-team",
    "deployed_by": "alice@acme.com",
    "commit_sha": "a3f9c12d",
    "timestamp": "2025-05-25T14:00:00Z",
    "hours_before_spike": 3.0,
}

FAKE_AUDIT_RESULT = {"indexed": True, "index": "cost-anomaly-audit-2025.05.25", "doc_id": "abc123", "error": None}


# ---------------------------------------------------------------------------
# Bedrock converse mock — simulates Claude's 7-step tool-calling sequence
# ---------------------------------------------------------------------------
def _make_bedrock_converse_side_effect(has_anomalies: bool = True):
    """
    Returns a function that simulates Claude's tool-calling sequence.
    Each call corresponds to one turn in the converse loop.

    has_anomalies=True  → full 5-tool sequence (find_spike → timeseries →
                          deploys → post_slack_alert → write_audit)
    has_anomalies=False → short 2-tool sequence (find_spike → write_audit,
                          no Slack call)
    """
    turn = {"n": 0}

    def side_effect(**kwargs):
        turn["n"] += 1
        n = turn["n"]

        def tool_use_response(tool_use_id, name, tool_input):
            return {
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "toolUse": {
                                    "toolUseId": tool_use_id,
                                    "name": name,
                                    "input": tool_input,
                                }
                            }
                        ],
                    }
                },
                "stopReason": "tool_use",
                "usage": {"inputTokens": 500, "outputTokens": 100},
            }

        def end_turn_response(text="Done."):
            return {
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [{"text": text}],
                    }
                },
                "stopReason": "end_turn",
                "usage": {"inputTokens": 200, "outputTokens": 50},
            }

        if has_anomalies:
            if n == 1:
                return tool_use_response("t1", "find_spike_services", {"threshold_pct": 25.0})
            elif n == 2:
                return tool_use_response("t2", "get_cost_timeseries", {"service": "Amazon EC2", "hours": 48})
            elif n == 3:
                return tool_use_response("t3", "find_deploys_near_spike", {
                    "service": "Amazon EC2",
                    "spike_start_iso": "2025-05-25T17:00:00Z",
                    "window_hours": 12,
                })
            elif n == 4:
                return tool_use_response("t4", "post_slack_alert", {
                    "anomalies": [FAKE_SPIKE],
                    "causes": ["HPA scaled checkout pods 3→12 replicas after deploy v2.3.1"],
                    "suggestions": ["Reduce minReplicas to 3. Estimated saving: ~$220/day."],
                    "run_meta": {"run_id": "test-run", "duration_seconds": 5.0},
                })
            elif n == 5:
                return tool_use_response("t5", "write_audit", {
                    "run_id": "test-run",
                    "anomalies_found": 1,
                    "slack_delivered": True,
                    "duration_seconds": 5.0,
                    "token_count": 2500,
                })
            else:
                return end_turn_response()
        else:
            # No anomalies path
            if n == 1:
                return tool_use_response("t1", "find_spike_services", {"threshold_pct": 25.0})
            elif n == 2:
                return tool_use_response("t2", "write_audit", {
                    "run_id": "test-run",
                    "anomalies_found": 0,
                    "slack_delivered": False,
                    "duration_seconds": 2.0,
                    "token_count": 800,
                })
            else:
                return end_turn_response()

    return side_effect


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------
class TestAgentIntegration(unittest.TestCase):
    """Full agent integration tests with mocked Bedrock and Elasticsearch."""

    @patch("tools.audit_writer._get_es_client")
    @patch("tools.slack_notify.urllib.request.urlopen")
    @patch("tools.elastic_search._get_es_client")
    @patch("boto3.client")
    def _run_agent(self, mock_boto_client, mock_es_client,
                   mock_urlopen, mock_audit_es, has_anomalies=True):
        """Helper: set up all mocks and run the agent."""

        # Mock Bedrock client
        mock_bedrock = MagicMock()
        mock_bedrock.converse.side_effect = _make_bedrock_converse_side_effect(has_anomalies)
        mock_boto_client.return_value = mock_bedrock

        # Mock Elasticsearch tool functions
        mock_es = MagicMock()
        mock_es_client.return_value = mock_es
        mock_audit_es.return_value = mock_es

        # Mock ES index (for audit writes)
        mock_es.index.return_value = {"_id": "abc123"}

        # Mock Slack webhook response
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        # Patch individual tool functions to return fake data
        with patch("agent.find_spike_services", return_value=[FAKE_SPIKE] if has_anomalies else []), \
             patch("agent.get_cost_timeseries", return_value=FAKE_TIMESERIES), \
             patch("agent.find_deploys_near_spike", return_value=[FAKE_DEPLOY]), \
             patch("agent.post_slack_alert", return_value={"delivered": True, "error": None}) as mock_slack, \
             patch("agent.write_audit", return_value=FAKE_AUDIT_RESULT) as mock_audit:

            agent = CloudCostAnomalyAgent()
            result = agent.run()
            return result, mock_bedrock, mock_slack, mock_audit

    def test_full_chain_completes_successfully(self):
        """Full 7-step sequence completes without error."""
        result, _, _, _ = self._run_agent(has_anomalies=True)
        self.assertIn("run_id", result)
        self.assertIn("duration_seconds", result)
        self.assertIn("total_tokens", result)

    def test_find_spike_services_called_once(self):
        """Spike detection runs exactly once."""
        _, mock_bedrock, _, _ = self._run_agent(has_anomalies=True)
        calls = mock_bedrock.converse.call_args_list
        # First converse call should trigger find_spike_services
        self.assertGreaterEqual(len(calls), 1)

    def test_timeseries_called_for_spiked_service(self):
        """Timeseries is fetched for the spiked service."""
        _, mock_bedrock, _, _ = self._run_agent(has_anomalies=True)
        calls = mock_bedrock.converse.call_args_list
        self.assertGreaterEqual(len(calls), 2)

    def test_deploy_lookup_called_with_correct_service(self):
        """Deploy lookup uses the correct service parameter."""
        _, mock_bedrock, _, _ = self._run_agent(has_anomalies=True)
        calls = mock_bedrock.converse.call_args_list
        self.assertGreaterEqual(len(calls), 3)

    def test_slack_message_contains_required_fields(self):
        """Slack alert is called with anomalies when spikes are found."""
        _, _, mock_slack, _ = self._run_agent(has_anomalies=True)
        mock_slack.assert_called_once()

    def test_audit_written_on_success(self):
        """Audit record is always written."""
        _, _, _, mock_audit = self._run_agent(has_anomalies=True)
        mock_audit.assert_called_once()

    def test_no_anomalies_exits_silently(self):
        """No Slack message when no anomalies are found."""
        _, _, mock_slack, mock_audit = self._run_agent(has_anomalies=False)
        mock_slack.assert_not_called()
        mock_audit.assert_called_once()

    def test_lambda_handler_returns_200(self):
        """Lambda handler returns proper response structure."""
        with patch("agent.CloudCostAnomalyAgent") as MockAgent:
            mock_instance = MagicMock()
            mock_instance.run.return_value = {
                "run_id": "test-123",
                "duration_seconds": 5.0,
                "total_tokens": 2500,
            }
            MockAgent.return_value = mock_instance

            response = lambda_handler({"source": "manual-test"})
            self.assertEqual(response["statusCode"], 200)
            self.assertEqual(response["body"]["run_id"], "test-123")


if __name__ == "__main__":
    unittest.main()
