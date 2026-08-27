"""
tests/test_native_agent.py — Unit test for AWS Native Plug & Play Agent and Athena CUR module.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/fake")
os.environ.setdefault("AWS_BEDROCK_REGION", "us-east-1")
os.environ.setdefault("COST_PROVIDER", "ATHENA_CUR")

from aws_native_plug_and_play.tools.aws_athena_cur import (
    find_spike_services,
    get_7day_baseline,
    get_todays_cost,
    get_cost_timeseries,
)


class TestAthenaCURModule(unittest.TestCase):
    """Test suite for Athena CUR / FOCUS module."""

    @patch("aws_native_plug_and_play.tools.aws_athena_cur.run_athena_query")
    def test_get_7day_baseline(self, mock_query):
        mock_query.return_value = [
            {"service": "AmazonEC2", "daily_avg_usd": "100.50"},
            {"service": "AmazonS3", "daily_avg_usd": "50.25"},
        ]
        result = get_7day_baseline()
        self.assertEqual(result, {"AmazonEC2": 100.50, "AmazonS3": 50.25})

    @patch("aws_native_plug_and_play.tools.aws_athena_cur.run_athena_query")
    def test_get_todays_cost(self, mock_query):
        mock_query.return_value = [
            {"service": "AmazonEC2", "todays_cost_usd": "150.75"},
            {"service": "AmazonS3", "todays_cost_usd": "55.00"},
        ]
        result = get_todays_cost()
        self.assertEqual(result, {"AmazonEC2": 150.75, "AmazonS3": 55.00})

    @patch("aws_native_plug_and_play.tools.aws_athena_cur.get_todays_cost")
    @patch("aws_native_plug_and_play.tools.aws_athena_cur.get_7day_baseline")
    def test_find_spike_services(self, mock_baseline, mock_today):
        mock_baseline.return_value = {"AmazonEC2": 100.0, "AmazonS3": 50.0}
        mock_today.return_value = {"AmazonEC2": 150.0, "AmazonS3": 52.0}

        spikes = find_spike_services(threshold_pct=25.0)
        self.assertEqual(len(spikes), 1)
        self.assertEqual(spikes[0]["service"], "AmazonEC2")
        self.assertEqual(spikes[0]["pct_change"], 50.0)

    @patch("aws_native_plug_and_play.tools.aws_athena_cur.run_athena_query")
    def test_get_cost_timeseries(self, mock_query):
        mock_query.return_value = [
            {"hour_ts": "2026-08-27 08:00:00.000", "hourly_cost": "12.50"},
            {"hour_ts": "2026-08-27 09:00:00.000", "hourly_cost": "15.75"},
        ]
        ts = get_cost_timeseries("AmazonEC2", hours=2)
        self.assertEqual(len(ts), 2)
        self.assertEqual(ts[0]["cost_usd"], 12.50)


if __name__ == "__main__":
    unittest.main()
