"""
tests/test_native_agent.py — Athena CUR module tests.

These mock the boto3 Athena and Glue clients rather than run_athena_query, so the
SQL the module actually generates is asserted. Mocking run_athena_query hides
schema bugs: the module can be incapable of executing a single query and still
pass every test.
"""

import os
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

os.environ.setdefault("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/fake")
os.environ.setdefault("AWS_BEDROCK_REGION", "us-east-1")
os.environ.setdefault("COST_PROVIDER", "ATHENA_CUR")

from aws_native_plug_and_play.tools import aws_athena_cur as cur  # noqa: E402
from aws_native_plug_and_play.tools.aws_athena_cur import (  # noqa: E402
    CostQueryError,
    CurSchema,
    find_spike_services,
    get_cost_timeseries,
)
from aws_native_plug_and_play.tools.slack_notify import (  # noqa: E402
    _as_text,
    SlackDeliveryError,
    post_slack_alert,
)


# The real column list from this account's aws-CUR-create-table.sql.
LEGACY_COLUMNS = [
    "identity_line_item_id",
    "bill_payer_account_id",
    "line_item_usage_account_id",
    "line_item_line_item_type",
    "line_item_usage_start_date",
    "line_item_usage_end_date",
    "line_item_product_code",
    "line_item_resource_id",
    "line_item_unblended_cost",
    "product_servicename",
]

FOCUS_COLUMNS = [
    "billingaccountid",
    "servicename",
    "billedcost",
    "chargeperiodstart",
    "chargecategory",
]


def _glue_table(columns, partition_keys=("year", "month")):
    return {
        "Table": {
            "StorageDescriptor": {"Columns": [{"Name": c} for c in columns]},
            "PartitionKeys": [{"Name": p} for p in partition_keys],
        }
    }


def _athena_client(rows):
    """Build a mock Athena client that returns `rows` (list of lists, incl. header)."""
    client = MagicMock()
    client.start_query_execution.return_value = {"QueryExecutionId": "q-test"}
    client.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
    }
    client.get_query_results.return_value = {
        "ResultSet": {
            "Rows": [{"Data": [{"VarCharValue": v} for v in row]} for row in rows]
        }
    }
    return client


class SchemaDetectionTest(unittest.TestCase):
    def setUp(self):
        cur._schema_cache = None

    def tearDown(self):
        cur._schema_cache = None

    def test_detects_legacy_cur(self):
        glue = MagicMock()
        glue.get_table.return_value = _glue_table(LEGACY_COLUMNS)
        with patch.object(cur.boto3, "client", return_value=glue):
            schema = cur.detect_schema()

        self.assertEqual(schema.flavor, "legacy")
        self.assertEqual(schema.service_col, "line_item_product_code")
        self.assertEqual(schema.cost_col, "line_item_unblended_cost")
        self.assertEqual(schema.time_col, "line_item_usage_start_date")
        self.assertEqual(schema.partition_style, "year_month")
        self.assertIn("line_item_line_item_type IN", schema.usage_filter)

    def test_detects_focus(self):
        glue = MagicMock()
        glue.get_table.return_value = _glue_table(
            FOCUS_COLUMNS, partition_keys=("billing_period",)
        )
        with patch.object(cur.boto3, "client", return_value=glue):
            schema = cur.detect_schema()

        self.assertEqual(schema.flavor, "focus")
        self.assertEqual(schema.service_col, "servicename")
        self.assertEqual(schema.partition_style, "billing_period")

    def test_unknown_schema_raises_instead_of_returning_empty(self):
        glue = MagicMock()
        glue.get_table.return_value = _glue_table(["some_col", "other_col"])
        with patch.object(cur.boto3, "client", return_value=glue):
            with self.assertRaises(CostQueryError) as ctx:
                cur.detect_schema()
        self.assertIn("neither legacy CUR nor FOCUS", str(ctx.exception))

    def test_missing_glue_table_raises(self):
        glue = MagicMock()
        glue.get_table.side_effect = Exception("EntityNotFoundException")
        with patch.object(cur.boto3, "client", return_value=glue):
            with self.assertRaises(CostQueryError) as ctx:
                cur.detect_schema()
        self.assertIn("crawler", str(ctx.exception).lower())


class GeneratedSqlTest(unittest.TestCase):
    """The regression tests for the schema bug: assert the SQL that goes to Athena."""

    def setUp(self):
        cur._schema_cache = CurSchema(
            flavor="legacy",
            service_col="line_item_product_code",
            cost_col="line_item_unblended_cost",
            time_col="line_item_usage_start_date",
            usage_filter=(
                "line_item_line_item_type IN "
                "('Usage', 'DiscountedUsage', 'SavingsPlanCoveredUsage')"
            ),
            partition_style="year_month",
        )

    def tearDown(self):
        cur._schema_cache = None

    def _capture_sql(self, fn, rows):
        client = _athena_client(rows)
        with patch.object(cur, "_get_athena_client", return_value=client):
            fn()
        return client.start_query_execution.call_args.kwargs

    def test_daily_costs_sql_targets_legacy_columns_only(self):
        kwargs = self._capture_sql(
            cur.get_daily_costs, [["service", "usage_day", "cost_usd"]]
        )
        sql = kwargs["QueryString"]

        # The original bug: these columns do not exist in a legacy CUR table, and
        # referencing them fails the whole query at analysis time.
        self.assertNotIn("ServiceName", sql)
        self.assertNotIn("BilledCost", sql)
        self.assertNotIn("ChargePeriodStart", sql)
        self.assertNotIn("COALESCE", sql)

        self.assertIn("line_item_product_code", sql)
        self.assertIn("line_item_unblended_cost", sql)

    def test_daily_costs_sql_prunes_partitions(self):
        kwargs = self._capture_sql(
            cur.get_daily_costs, [["service", "usage_day", "cost_usd"]]
        )
        sql = kwargs["QueryString"]
        self.assertIn("year = ", sql)
        self.assertIn("month IN ", sql)

    def test_daily_costs_sql_excludes_tax_and_credits(self):
        kwargs = self._capture_sql(
            cur.get_daily_costs, [["service", "usage_day", "cost_usd"]]
        )
        self.assertIn("line_item_line_item_type IN", kwargs["QueryString"])

    def test_timeseries_passes_service_as_parameter_not_string(self):
        client = _athena_client([["hour_ts", "hourly_cost"]])
        with patch.object(cur, "_get_athena_client", return_value=client):
            get_cost_timeseries("AmazonEC2", hours=48)
        kwargs = client.start_query_execution.call_args.kwargs

        self.assertEqual(kwargs["ExecutionParameters"], ["AmazonEC2"])
        self.assertIn("= ?", kwargs["QueryString"])
        self.assertNotIn("AmazonEC2", kwargs["QueryString"])

    def test_timeseries_rejects_injection_in_service_name(self):
        with self.assertRaises(ValueError):
            get_cost_timeseries("EC2'; DROP TABLE aws_cur;--", hours=48)

    def test_partition_predicate_spans_month_boundary(self):
        predicate = cur._partition_predicate(
            cur._schema_cache, date(2026, 8, 28), date(2026, 9, 3)
        )
        self.assertIn("year = '2026'", predicate)
        self.assertIn("'8'", predicate)
        self.assertIn("'9'", predicate)
        # Unpadded and padded partition values are both matched.
        self.assertIn("'09'", predicate)

    def test_failed_query_raises_rather_than_returning_empty(self):
        client = _athena_client([])
        client.get_query_execution.return_value = {
            "QueryExecution": {
                "Status": {
                    "State": "FAILED",
                    "StateChangeReason": "COLUMN_NOT_FOUND: servicename",
                }
            }
        }
        with patch.object(cur, "_get_athena_client", return_value=client):
            with self.assertRaises(CostQueryError) as ctx:
                cur.run_athena_query("SELECT 1")
        self.assertIn("COLUMN_NOT_FOUND", str(ctx.exception))


class SpikeDetectionTest(unittest.TestCase):
    def setUp(self):
        self.today = datetime.now(timezone.utc).date()
        self.yesterday = self.today - timedelta(days=1)

    def _series(self, baseline, current, days=7):
        series = {
            self.yesterday - timedelta(days=i): baseline for i in range(1, days + 1)
        }
        series[self.yesterday] = current
        return series

    def test_flags_service_above_threshold(self):
        daily = {
            "AmazonEC2": self._series(baseline=100.0, current=150.0),
            "AmazonS3": self._series(baseline=50.0, current=52.0),
        }
        with patch.object(cur, "get_daily_costs", return_value=daily):
            spikes = find_spike_services(threshold_pct=25.0)

        self.assertEqual(len(spikes), 1)
        self.assertEqual(spikes[0]["service"], "AmazonEC2")
        self.assertEqual(spikes[0]["pct_change"], 50.0)
        self.assertEqual(spikes[0]["current_usd"], 150.0)
        self.assertEqual(spikes[0]["as_of"], self.yesterday.isoformat())

    def test_ignores_partial_current_day(self):
        """Today is always partial in CUR — including it would suppress every spike."""
        daily = {"AmazonEC2": self._series(baseline=100.0, current=150.0)}
        daily["AmazonEC2"][self.today] = 3.0  # a few hours of partial data

        with patch.object(cur, "get_daily_costs", return_value=daily):
            spikes = find_spike_services(threshold_pct=25.0)

        self.assertEqual(len(spikes), 1)
        self.assertEqual(spikes[0]["as_of"], self.yesterday.isoformat())

    def test_ignores_trivial_baselines(self):
        daily = {"AmazonRoute53": self._series(baseline=0.40, current=0.90)}
        with patch.object(cur, "get_daily_costs", return_value=daily):
            self.assertEqual(find_spike_services(threshold_pct=25.0), [])

    def test_new_service_with_zero_baseline_is_flagged(self):
        """A brand-new service (baseline $0) must be caught on an absolute floor —
        the percentage path divides by zero and would otherwise skip it."""
        daily = {"AmazonEC2": {self.yesterday: 12.50}}
        for i in range(1, 8):
            daily["AmazonEC2"][self.yesterday - timedelta(days=i)] = 0.0

        with patch.object(cur, "NEW_SERVICE_USD", 5.0), \
                patch.object(cur, "get_daily_costs", return_value=daily):
            spikes = find_spike_services(threshold_pct=25.0)

        self.assertEqual(len(spikes), 1)
        self.assertTrue(spikes[0]["is_new_service"])
        self.assertIsNone(spikes[0]["pct_change"])
        self.assertEqual(spikes[0]["current_usd"], 12.50)

    def test_new_service_below_absolute_floor_ignored(self):
        daily = {"AmazonEC2": {self.yesterday: 0.30}}
        for i in range(1, 8):
            daily["AmazonEC2"][self.yesterday - timedelta(days=i)] = 0.0

        with patch.object(cur, "NEW_SERVICE_USD", 5.0), \
                patch.object(cur, "get_daily_costs", return_value=daily):
            self.assertEqual(find_spike_services(threshold_pct=25.0), [])

    def test_no_data_raises_rather_than_reporting_no_anomalies(self):
        with patch.object(cur, "get_daily_costs", return_value={}):
            with self.assertRaises(CostQueryError):
                find_spike_services(threshold_pct=25.0)

    def test_only_partial_today_raises(self):
        daily = {"AmazonEC2": {self.today: 12.0}}
        with patch.object(cur, "get_daily_costs", return_value=daily):
            with self.assertRaises(CostQueryError) as ctx:
                find_spike_services(threshold_pct=25.0)
        self.assertIn("no complete day", str(ctx.exception).lower())

    def test_sorted_by_dollar_delta(self):
        daily = {
            "Small": self._series(baseline=10.0, current=20.0),      # +$10
            "Large": self._series(baseline=200.0, current=400.0),    # +$200
        }
        with patch.object(cur, "get_daily_costs", return_value=daily):
            spikes = find_spike_services(threshold_pct=25.0)
        self.assertEqual([s["service"] for s in spikes], ["Large", "Small"])


class SlackDeliveryTest(unittest.TestCase):
    ANOMALY = {
        "service": "AmazonEC2",
        "team": "AWS Account",
        "as_of": "2026-08-31",
        "current_usd": 847.20,
        "baseline_usd": 592.10,
        "delta_usd": 255.10,
        "pct_change": 43.1,
    }

    def test_missing_webhook_raises(self):
        with patch.dict(os.environ, {"SLACK_WEBHOOK_URL": ""}):
            with self.assertRaises(SlackDeliveryError):
                post_slack_alert([self.ANOMALY], ["cause"], ["fix"], {"run_id": "r1"})

    def test_refuses_to_post_empty_alert(self):
        with self.assertRaises(SlackDeliveryError):
            post_slack_alert([], [], [], {"run_id": "r1"})

    def test_non_200_raises(self):
        response = MagicMock()
        response.status = 500
        response.__enter__ = lambda s: s
        response.__exit__ = lambda *a: False
        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(SlackDeliveryError):
                post_slack_alert([self.ANOMALY], ["cause"], ["fix"], {"run_id": "r1"})

    def test_successful_post(self):
        response = MagicMock()
        response.status = 200
        response.__enter__ = lambda s: s
        response.__exit__ = lambda *a: False
        with patch("urllib.request.urlopen", return_value=response):
            result = post_slack_alert(
                [self.ANOMALY], ["cause"], ["fix"], {"run_id": "r1", "duration_seconds": 4.2}
            )
        self.assertTrue(result["delivered"])
        self.assertEqual(result["anomaly_count"], 1)

    def test_new_service_anomaly_renders_without_percentage(self):
        """A new-service anomaly has pct_change=None; the card must not crash on it."""
        from aws_native_plug_and_play.tools.slack_notify import _build_anomaly_section

        anomaly = {
            "service": "AmazonEC2", "team": "AWS Account", "as_of": "2026-09-02",
            "current_usd": 12.50, "baseline_usd": 0.0, "delta_usd": 12.50,
            "pct_change": None, "is_new_service": True,
        }
        blocks = _build_anomaly_section(anomaly, "New EC2 instance launched.", "Terminate it.")
        rendered = str(blocks)
        self.assertIn("new cost source", rendered)
        self.assertNotIn("None%", rendered)


class CauseTextCoercionTest(unittest.TestCase):
    """
    The model is asked for plain strings but a tool schema cannot guarantee it —
    observed live returning {"summary": ...} / {"action": ...} objects, which
    rendered as str(dict) in the Slack card.
    """

    def test_passes_strings_through(self):
        self.assertEqual(_as_text("  HPA scaled to 12 replicas.  "),
                         "HPA scaled to 12 replicas.")

    def test_extracts_summary_from_object(self):
        self.assertEqual(
            _as_text({"service": "EC2", "summary": "HPA scaled 3->12.", "n": 1}),
            "HPA scaled 3->12.",
        )

    def test_extracts_action_from_object(self):
        self.assertEqual(
            _as_text({"action": "Cap maxReplicas to 4.", "saving_usd": 255.1}),
            "Cap maxReplicas to 4.",
        )

    def test_falls_back_to_longest_string_not_dict_dump(self):
        result = _as_text({"foo": "short", "bar": "a much longer explanation here"})
        self.assertEqual(result, "a much longer explanation here")
        self.assertNotIn("{", result)

    def test_joins_lists(self):
        self.assertEqual(_as_text(["First part.", "Second part."]),
                         "First part. Second part.")


class RunMetaTest(unittest.TestCase):
    """Run telemetry is measured by the agent, never taken from the model."""

    @staticmethod
    def _import_native_agent():
        """
        Import aws_native_plug_and_play/agent.py the way Lambda does.

        Both the repo root and aws_native_plug_and_play/ contain a `tools`
        package, so `tools` resolves to whichever was imported first. Isolate the
        name for the duration of this import and put it back afterwards.
        """
        import sys

        pkg_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "aws_native_plug_and_play")
        )
        clashing = ("tools", "agent")
        saved = {
            name: module
            for name, module in sys.modules.items()
            if name in clashing or name.startswith("tools.")
        }
        for name in saved:
            del sys.modules[name]

        sys.path.insert(0, pkg_dir)
        try:
            import agent as agent_module
            return agent_module
        finally:
            sys.path.remove(pkg_dir)
            for name in [
                n for n in list(sys.modules)
                if n in clashing or n.startswith("tools.")
            ]:
                del sys.modules[name]
            sys.modules.update(saved)

    def test_agent_overrides_model_supplied_run_meta(self):
        agent_module = self._import_native_agent()

        captured = {}
        original = agent_module.TOOL_DISPATCH["post_slack_alert"]
        agent_module.TOOL_DISPATCH["post_slack_alert"] = (
            lambda a: captured.update(a["run_meta"]) or {"delivered": True}
        )
        try:
            agent = agent_module.NativeAWSFinOpsAgent.__new__(
                agent_module.NativeAWSFinOpsAgent
            )
            agent.run_id = "real-run-id"
            agent.start_time = 1000.0
            agent._dispatch_tool(
                "post_slack_alert",
                {
                    "anomalies": [], "causes": [], "suggestions": [],
                    # What the model guessed:
                    "run_meta": {"run_id": "hallucinated", "duration_seconds": 0.0},
                },
            )
        finally:
            agent_module.TOOL_DISPATCH["post_slack_alert"] = original

        self.assertEqual(captured["run_id"], "real-run-id")
        self.assertGreater(captured["duration_seconds"], 0.0)


if __name__ == "__main__":
    unittest.main()
