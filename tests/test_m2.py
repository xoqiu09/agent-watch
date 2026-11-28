import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from agentwatch.__main__ import main
from tests.test_m1 import call, event_ids, events_from


DAY = "2026-11-28"


def second_day(number, *args, **kwargs):
    return call(number, *args, ts=f"{DAY}T00:00:{number:02d}+08:00", **kwargs)


class TestM2(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.log = self.directory / "calls.jsonl"
        self.config = self.directory / "config.json"
        self.output = self.directory / "report.md"
        self.set_prices({"model-a": {"in_per_1k": 1, "out_per_1k": 4}})

    def set_prices(self, prices):
        self.config.write_text(json.dumps({
            "timeout_streak": 2,
            "daily_token_budget": {"alice": 10},
            "default_daily_token_budget": 100,
            "prices": prices,
        }), encoding="utf-8")

    def write_rows(self, *rows):
        with self.log.open("w", encoding="utf-8") as target:
            for row in rows:
                print(row if isinstance(row, str) else json.dumps(row), file=target)

    def cli(self, command, *args):
        result = subprocess.run(
            [sys.executable, "-m", "agentwatch", command, *map(str, args)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def summary(self):
        return json.loads(self.cli("summary", "--log", self.log, "--config", self.config))

    def report(self):
        self.cli("report", "--log", self.log, "--config", self.config,
                 "--date", DAY, "--out", self.output)
        return self.output.read_text(encoding="utf-8")

    def test_summary_rejects_duplicate_request_ids_before_printing_costs(self):
        row = call(1, tokens_in=1000)
        self.write_rows(row, row)
        result = subprocess.run(
            [sys.executable, "-m", "agentwatch", "summary",
             "--log", str(self.log), "--config", str(self.config)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout, "")
        self.assertIn("duplicate request_id 'r1' on lines 1 and 2", result.stderr)

    def test_summary_rejects_negative_prices_and_accepts_zero(self):
        self.write_rows(call(1, tokens_in=1000, tokens_out=1000))
        zero_prices = {"in_per_1k": 0, "out_per_1k": 0}
        self.set_prices({"model-a": zero_prices})
        summary = self.summary()
        self.assertEqual(summary["by_user"]["alice"]["cost"], 0)
        self.assertEqual(summary["by_model"]["model-a"]["cost"], 0)

        for field, price in (("in_per_1k", -1), ("out_per_1k", -0.5)):
            with self.subTest(field=field):
                self.set_prices({"model-a": {**zero_prices, field: price}})
                result = subprocess.run(
                    [sys.executable, "-m", "agentwatch", "summary",
                     "--log", str(self.log), "--config", str(self.config)],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(result.stdout, "")
                self.assertIn(f"prices['model-a'].{field} must be non-negative", result.stderr)

    def test_cost_is_grouped_and_rounded_only_after_adding_calls(self):
        self.set_prices({
            "model-a": {"in_per_1k": 0.04, "out_per_1k": 0.03},
            "model-b": {"in_per_1k": 2.5, "out_per_1k": 5},
        })
        self.write_rows(
            call(1, tokens_in=1), call(2, tokens_in=1),
            call(3, "error", user="bob", tokens_in=1, tokens_out=1),
            call(4, model="model-b", tokens_in=120, tokens_out=340),
        )
        summary = self.summary()
        # Model A has 0.00004 + 0.00004 + 0.00007 yuan; model B has 2 yuan.
        self.assertEqual({key: item["cost"] for key, item in summary["by_model"].items()},
                         {"model-a": 0.0002, "model-b": 2.0})
        self.assertEqual({key: item["cost"] for key, item in summary["by_user"].items()},
                         {"alice": 2.0001, "bob": 0.0001})

    def test_cost_retains_fourth_decimal_place_in_all_outputs(self):
        self.set_prices({"model-a": {"in_per_1k": 1000, "out_per_1k": 0.1}})
        self.write_rows(second_day(1, tokens_in=1099511627776, tokens_out=1))
        expected = Decimal("1099511627776.0001")
        summary = json.loads(self.cli("summary", "--log", self.log, "--config", self.config),
                             parse_float=Decimal)
        self.assertEqual(summary["by_user"]["alice"]["cost"], expected)
        self.assertEqual(summary["by_model"]["model-a"]["cost"], expected)
        self.assertIn(f"| {expected} 元 |", self.report())

        export = self.directory / "demo.json"
        self.cli("export", "--log", self.log, "--config", self.config, "--out", export)
        payload = json.loads(export.read_text(encoding="utf-8"), parse_float=Decimal)
        self.assertEqual(payload["summary"]["by_model"]["model-a"]["cost"], expected)
        self.assertEqual(payload["report"]["metrics"]["cost"], expected)

    def _run_readme_display(self):
        export = self.directory / "demo.json"
        self.cli("export", "--log", self.log, "--config", self.config, "--out", export)
        readme = Path(__file__).resolve().parents[1] / "README.md"
        snippet = readme.read_text(encoding="utf-8").split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        return subprocess.run(
            [sys.executable, "-", str(export)], input=snippet, capture_output=True, text=True,
        )

    def test_readme_display_retains_fourth_decimal_place(self):
        self.set_prices({"model-a": {"in_per_1k": 1000, "out_per_1k": 0.1}})
        self.write_rows(second_day(1, tokens_in=1099511627776, tokens_out=1))

        result = self._run_readme_display()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "model-a 1099511627776.0001", "日报费用 1099511627776.0001",
        ])

    def test_readme_display_handles_an_empty_export(self):
        self.log.touch()

        result = self._run_readme_display()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "没有日报\n")

    def test_cost_rounds_exact_half_up(self):
        self.set_prices({"model-a": {"in_per_1k": 0.05, "out_per_1k": 0}})
        self.write_rows(call(1, tokens_in=1))
        # 1 token at 0.05 yuan per thousand costs exactly 0.00005 yuan.
        summary = self.summary()
        self.assertEqual(summary["by_user"]["alice"]["cost"], 0.0001)
        self.assertEqual(summary["by_model"]["model-a"]["cost"], 0.0001)

    def test_unpriced_calls_are_marked_and_excluded_from_totals(self):
        self.write_rows(
            call(1, tokens_in=1000, tokens_out=500),
            call(2, model="unknown", tokens_out=9000),
            call(3, user="bob"),
            second_day(4, model="unknown", user="carol", tokens_out=10000),
        )
        summary = self.summary()
        models, users = summary["by_model"], summary["by_user"]
        self.assertEqual(models["unknown"]["cost"], "未定价")
        self.assertEqual((users["alice"]["cost"], users["alice"]["unpriced_models"]),
                         (3.0, ["unknown"]))
        self.assertEqual(users["bob"]["cost"], 0.0)
        self.assertEqual(users["carol"]["cost"], "未定价")
        self.assertIn(f"| {DAY} | 1 | 100.00% | 100 ms | 0 | 10000 | 未定价 |", self.report())

    def test_p95_requires_more_than_thirty_percent_growth(self):
        for latency, flagged in ((130.129, False), (130.13, False), (130.131, True)):
            with self.subTest(latency=latency):
                self.write_rows(call(1, latency=100.1), second_day(2, latency=latency))
                self.assertEqual("变慢" in self.report(), flagged)

    def test_p95_compares_exact_json_decimals_at_thirty_percent(self):
        yesterday = json.dumps(call(1, latency=0))
        today = json.dumps(second_day(2, latency=0))
        self.write_rows(
            yesterday.replace('"latency_ms": 0,', '"latency_ms": 100.0000000000001,'),
            today.replace('"latency_ms": 0,', '"latency_ms": 130.00000000000013,'),
        )
        self.assertIn("无指标超过门槛", self.report())

    def test_success_requires_a_drop_of_more_than_five_points(self):
        yesterday = [call(i) for i in range(1, 21)]
        for failures, flagged in ((0, False), (1, False), (2, True)):
            with self.subTest(failures=failures):
                today = [second_day(i, "error" if i < 21 + failures else "ok")
                         for i in range(21, 41)]
                self.write_rows(*yesterday, *today)
                self.assertEqual("失败变多" in self.report(), flagged)

    def test_success_compares_points_from_a_partial_success_rate(self):
        yesterday = [call(i, "ok" if i <= 25 else "error") for i in range(1, 51)]
        today = [second_day(i, "ok" if i <= 23 else "error") for i in range(1, 51)]
        for row in today:
            row["request_id"] += "-next"
        self.write_rows(*yesterday, *today)
        self.assertIn("无指标超过门槛", self.report())

    def test_cost_requires_more_than_fifty_percent_growth_before_rounding(self):
        self.set_prices({"model-a": {"in_per_1k": 0.01, "out_per_1k": 0}})
        for tokens, flagged in ((8, False), (9, False), (10, True)):
            with self.subTest(tokens=tokens):
                self.write_rows(call(1, tokens_in=6), second_day(2, tokens_in=tokens))
                report = self.report()
                self.assertEqual(report.count("0.0001 元"), 2)
                self.assertEqual("费用上涨" in report, flagged)

    def test_report_shows_both_costs_without_flagging_growth_from_zero(self):
        self.set_prices({"model-a": {"in_per_1k": 1.25, "out_per_1k": 4}})
        self.write_rows(call(1), second_day(2, tokens_in=1000))
        report = self.report()
        self.assertIn(f"| {DAY} | 1 | 100.00% | 100 ms | 1000 | 0 | 1.2500 元 |", report)
        self.assertIn("| 2026-11-27 | 1 | 100.00% | 100 ms | 0 | 0 | 0.0000 元 |", report)
        self.assertNotIn("费用上涨", report)

    def test_missing_previous_day_does_not_compare_with_an_older_day(self):
        self.write_rows(call(1, ts="2026-11-26T12:00:00+08:00"),
                        second_day(2, latency=500, tokens_out=20))
        report = self.report()
        self.assertIn("没有前一天的数据", report)
        for flag in ("变慢", "失败变多", "费用上涨"):
            self.assertNotIn(flag, report)
        self.assertNotIn("2026-11-26", report)

    def test_report_recomputes_only_its_days_alerts_without_touching_state(self):
        self.write_rows(
            call(1, "timeout"), call(2, "timeout"), second_day(3),
            second_day(4, "timeout", "model-b", tokens_in=11),
            second_day(5, "timeout", "model-b"), second_day(6, "ok", "model-b"),
        )
        state = self.directory / "state.json"
        args = ("--log", self.log, "--config", self.config, "--state", state)
        self.cli("alerts", *args)
        before = state.read_bytes()
        stat = state.stat()

        report = self.report()
        self.assertEqual(state.read_bytes(), before)
        self.assertEqual((state.stat().st_ino, state.stat().st_mtime_ns),
                         (stat.st_ino, stat.st_mtime_ns))
        self.assertEqual(report.split("## 当天告警\n\n")[1].splitlines(), [
            f"- {DAY}T00:00:04+08:00 alice 用量超额（11 / 10 token），请求 r4",
            f"- {DAY}T00:00:05+08:00 model-b 连续超时，请求 r4, r5",
            f"- {DAY}T00:00:06+08:00 model-b 恢复，请求 r6",
        ])
        self.assertEqual(self.cli("alerts", *args), "")

    def test_report_explains_empty_alerts_when_the_live_streak_crosses_midnight(self):
        self.write_rows(
            call(1, "timeout", ts="2026-11-27T23:59:59+08:00"),
            second_day(2, "timeout"), second_day(3),
        )
        stdout = self.cli("alerts", "--log", self.log, "--config", self.config,
                          "--state", self.directory / "state.json")
        self.assertEqual(event_ids(events_from(stdout)), [
            ("timeout_streak", ["r1", "r2"]), ("recovered", ["r3"]),
        ])
        alert_section = self.report().split("## 当天告警\n\n")[1].strip()
        self.assertEqual(alert_section, "仅按当天调用重算，未触发告警")

    def test_report_replaces_output_links_without_overwriting_state(self):
        self.write_rows(second_day(1))
        for link in (self.output.hardlink_to, self.output.symlink_to):
            with self.subTest(link=link.__name__):
                state = self.directory / f"{link.__name__}.json"
                self.cli("alerts", "--log", self.log, "--config", self.config, "--state", state)
                before = state.read_bytes()
                self.output.unlink(missing_ok=True)
                link(state)
                self.assertTrue(self.report().startswith(f"# 调用日报 {DAY}\n"))
                self.assertEqual(state.read_bytes(), before)
                self.assertFalse(self.output.samefile(state))

    def test_report_does_not_read_alert_state(self):
        self.write_rows(second_day(1))
        state = self.directory / "state.json"
        self.cli("alerts", "--log", self.log, "--config", self.config, "--state", state)
        argv = ["agentwatch", "report", "--log", str(self.log), "--config", str(self.config),
                "--date", DAY, "--out", str(self.output)]
        open_path = Path.open

        def guard_state(path, *args, **kwargs):
            self.assertNotEqual(path, state, "report opened alert state")
            return open_path(path, *args, **kwargs)

        with (
            patch.object(sys, "argv", argv),
            patch.object(Path, "open", guard_state),
            patch("agentwatch.__main__.read_state", side_effect=AssertionError("read state")),
        ):
            self.assertEqual(main(), 0)

    def test_report_keeps_identifiers_on_their_alert_line(self):
        budget = second_day(1, user="alice\n- **user**", tokens_in=101)
        budget["request_id"] = "r1\n- [request]"
        model = "model-b\u2028- <model>"
        self.write_rows(budget, second_day(2, "timeout", model), second_day(3, "timeout", model))
        lines = self.report().split("## 当天告警\n\n")[1].splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn(r"alice - \*\*user\*\*", lines[0])
        self.assertTrue(lines[0].endswith(r"请求 r1 - \[request\]"))
        self.assertIn(r"model-b - \<model\>", lines[1])

    def test_export_includes_aliased_costs_and_the_reports_comparison(self):
        model = "private-model.example"
        user = "private-user"
        self.set_prices({model: {"in_per_1k": 1, "out_per_1k": 4}})
        self.write_rows(
            second_day(3, user=user, model=model, latency=131, tokens_in=151),
            call(1, user=user, model=model, tokens_in=100),
            second_day(2, user=user, model="private-unpriced.example"),
        )
        export = self.directory / "demo.json"
        self.cli("export", "--log", self.log, "--config", self.config, "--out", export)
        raw = export.read_text(encoding="utf-8")
        for private in (model, user, "private-unpriced.example"):
            self.assertNotIn(private, raw)
        payload = json.loads(raw)
        summary = payload["summary"]
        self.assertIn("cost", summary["by_model"]["model-1"])
        self.assertEqual(summary["by_model"]["model-1"]["cost"], 0.251)
        self.assertEqual(summary["by_model"]["model-2"]["cost"], "未定价")
        self.assertEqual(summary["by_user"]["user-1"].get("cost"), 0.251)
        self.assertEqual(summary["by_user"]["user-1"]["unpriced_models"], ["model-2"])
        report = payload["report"]
        self.assertIsNotNone(report)
        self.assertEqual(report["date"], DAY)
        self.assertEqual(report["metrics"]["cost"], 0.151)
        self.assertEqual(report["comparison"]["previous"]["cost"], 0.1)
        self.assertEqual(report["comparison"]["flags"], ["变慢", "费用上涨"])
        self.assertIn("变慢、费用上涨", self.report())

    def test_export_skips_aliases_that_match_private_identifiers(self):
        row = second_day(1, model="model-1", tokens_in=11)
        row["request_id"] = "request-1"
        self.write_rows(row)
        export = self.directory / "demo.json"
        self.cli("export", "--log", self.log, "--config", self.config, "--out", export)
        raw = export.read_text(encoding="utf-8")
        for private in ("model-1", "request-1"):
            self.assertNotIn(json.dumps(private), raw)
        payload = json.loads(raw)
        self.assertEqual(payload["summary"]["by_user"]["user-1"]["unpriced_models"], ["model-2"])
        event, = payload["alerts"]
        self.assertEqual(event["request_ids"], ["request-2"])
        self.assertEqual(event["records"][0]["request_id"], "request-2")
        self.assertEqual(event["records"][0]["model"], "model-2")

    def test_sample_produces_two_days_for_comparison(self):
        sample_config = Path(__file__).resolve().parents[1] / "config.json"
        self.config.write_bytes(sample_config.read_bytes())
        self.cli("sample", "--seed", 7, "--out", self.log)
        report = self.report()
        self.assertIn("| 2026-11-27 | 200 |", report)
        self.assertIn(f"| {DAY} | 5 | 40.00% | 2600 ms | 50 | 20020 | 80.1425 元 |", report)
        self.assertIn("变慢、失败变多、费用上涨", report)
