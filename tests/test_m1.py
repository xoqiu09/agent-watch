import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

START = datetime.fromisoformat("2026-11-27T10:00:00+08:00")


def call(
    number, status="ok", model="model-a", *, user="alice", ts=None,
    latency=100, tokens_in=0, tokens_out=0,
):
    return {
        "ts": ts or (START + timedelta(seconds=number)).isoformat(),
        "request_id": f"r{number}",
        "user": user,
        "model": model,
        "latency_ms": latency,
        "status": status,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }


def events_from(stdout):
    return [json.loads(line) for line in stdout.splitlines()]


def event_ids(events):
    return [(event["type"], event["request_ids"]) for event in events]


class TestM1(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.log = self.directory / "calls.jsonl"
        self.config = self.directory / "config.json"
        self.state = self.directory / "state.json"
        self.config.write_text(json.dumps({
            "timeout_streak": 2,
            "daily_token_budget": {"alice": 10},
            "default_daily_token_budget": 100,
        }), encoding="utf-8")

    def write_rows(self, *rows, append=False):
        with self.log.open("a" if append else "w", encoding="utf-8") as target:
            for row in rows:
                print(row if isinstance(row, str) else json.dumps(row), file=target)

    def cli(self, command, *args, exit_code=0):
        result = subprocess.run(
            [sys.executable, "-m", "agentwatch", command, "--log", str(self.log), *map(str, args)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, exit_code, result.stderr)
        return result

    def alerts(self, *, exit_code=0):
        return self.cli("alerts", "--config", self.config, "--state", self.state,
                        exit_code=exit_code)

    def test_01_success_resets_streak_and_recovers(self):
        actual = []
        for row in (call(1, "timeout"), call(2), call(3, "timeout"), call(4, "timeout"), call(5)):
            self.write_rows(row, append=True)
            actual.append(event_ids(events_from(self.alerts().stdout)))
        self.assertEqual(actual, [
            [], [], [], [("timeout_streak", ["r3", "r4"])], [("recovered", ["r5"])],
        ])

    def test_02_interleaved_models_keep_separate_streaks(self):
        self.write_rows(
            call(4, "timeout"), call(1, "timeout"), call(2, "timeout", "model-b"),
            call(3, "error"), call(5, "timeout", "model-b"), call(6, "timeout"), call(7),
        )
        events = events_from(self.alerts().stdout)
        self.assertEqual(
            [(event["type"], event["model"], event["request_ids"]) for event in events],
            [
                ("timeout_streak", "model-a", ["r1", "r4"]),
                ("timeout_streak", "model-b", ["r2", "r5"]),
                ("recovered", "model-a", ["r7"]),
            ],
        )

    def test_03_budget_boundary_repeat_and_local_midnight(self):
        self.write_rows(
            call(1, ts="2026-11-27T23:59:57+08:00", tokens_in=6, tokens_out=4),
            call(2, "error", ts="2026-11-27T23:59:58+08:00", tokens_out=1),
            call(3, ts="2026-11-27T23:59:59+08:00", tokens_in=1),
            call(4, ts="2026-11-28T00:00:00+08:00", tokens_in=10),
            call(5, ts="2026-11-28T00:00:01+08:00", tokens_out=1),
            call(6, user="bob", ts="2026-11-28T00:00:02+08:00", tokens_out=100),
            call(7, user="bob", ts="2026-11-28T00:00:03+08:00", tokens_in=1),
        )
        events = events_from(self.alerts().stdout)
        self.assertEqual(event_ids(events), [
            ("budget_exceeded", ["r2"]), ("budget_exceeded", ["r5"]),
            ("budget_exceeded", ["r7"]),
        ])
        self.assertEqual(
            [(event["user"], event["date"], event["tokens"], event["budget"]) for event in events],
            [("alice", "2026-11-27", 11, 10), ("alice", "2026-11-28", 11, 10),
             ("bob", "2026-11-28", 101, 100)],
        )

    def test_04_bad_rows_report_physical_lines_and_keep_valid_rows(self):
        missing = call(2)
        del missing["tokens_in"]
        self.write_rows(call(1), "not json", missing, call(3, "unknown"), call(4))
        result = self.cli("summary")
        messages = result.stderr.splitlines()
        self.assertEqual([message.split(":", 1)[0] for message in messages],
                         ["line 2", "line 3", "line 4"])
        self.assertIn("Expecting value", messages[0])
        self.assertIn("missing field: tokens_in", messages[1])
        self.assertIn("unknown status", messages[2])
        summary = json.loads(result.stdout)
        self.assertEqual(summary["bad_lines"], 3)
        self.assertEqual(summary["by_user"]["alice"]["calls"], 2)

    def test_05_alerts_replay_only_appended_rows(self):
        self.write_rows(call(1, "timeout", tokens_in=11), "not json")
        first = self.alerts()
        self.assertEqual(event_ids(events_from(first.stdout)), [("budget_exceeded", ["r1"])])
        self.assertIn("line 2:", first.stderr)
        second = self.alerts()
        self.assertEqual((second.stdout, second.stderr), ("", ""))

        self.write_rows(call(2, "timeout", tokens_out=1),
                        call(3, "timeout"), call(4), append=True)
        self.assertEqual(event_ids(events_from(self.alerts().stdout)), [
            ("timeout_streak", ["r1", "r2"]), ("recovered", ["r4"]),
        ])
        self.assertEqual(json.loads(self.state.read_text())["processed_lines"], 5)
        self.assertEqual(self.alerts().stdout, "")

    def test_06_summary_percentiles_use_nearest_rank_and_only_successes(self):
        successful = [call(i, latency=i * 10, tokens_in=3, tokens_out=5)
                      for i in range(20, 0, -1)]
        self.write_rows(
            *successful,
            call(21, "timeout", latency=9000, tokens_in=2),
            call(22, "error", "model-b", latency=1, tokens_out=3),
        )
        summary = json.loads(self.cli("summary").stdout)
        self.assertEqual(summary["by_user"]["alice"], {
            "calls": 22, "success_rate": 20 / 22, "timeouts": 1, "errors": 1,
            "p50_ms": 100, "p95_ms": 190, "tokens_in": 62, "tokens_out": 103,
        })
        self.assertEqual(summary["by_model"]["model-a"]["p95_ms"], 190)
        self.assertIsNone(summary["by_model"]["model-b"]["p50_ms"])
        self.assertIsNone(summary["by_model"]["model-b"]["p95_ms"])

    def test_07_trace_retrieves_the_original_alert_rows(self):
        originals = [" " + json.dumps(call(i, "timeout")) + "  " for i in (1, 2)]
        self.write_rows(*originals)
        event, = events_from(self.alerts().stdout)
        self.assertEqual(event["request_ids"], ["r1", "r2"])
        self.assertEqual(
            [self.cli("trace", request_id).stdout for request_id in event["request_ids"]],
            [row + "\n" for row in originals],
        )
        missing = self.cli("trace", "absent", exit_code=1)
        self.assertIn("not found: absent", missing.stderr)

    def test_08_export_is_private_and_byte_repeatable(self):
        record = call(1, "timeout", user="private-alice", tokens_in=101)
        record.update(hostname="private-host.example", path="/private/log/calls.jsonl")
        self.write_rows(record, call(2, "timeout", user="private-bob"),
                        call(3, user="private-alice"), "bad")
        first = self.directory / "first.json"
        second = self.directory / "second.json"
        self.cli("export", "--config", self.config, "--out", first)
        other_log = self.directory / "renamed.jsonl"
        self.log.rename(other_log)
        self.log = other_log
        self.cli("export", "--config", self.config, "--out", second)

        payload = first.read_bytes()
        self.assertEqual(payload, second.read_bytes())
        for private in (b"private-alice", b"private-bob", b"private-host.example",
                        b"/private/log/calls.jsonl", str(self.directory).encode()):
            self.assertNotIn(private, payload)
        report = json.loads(payload)
        summary = json.loads(self.cli("summary").stdout)
        self.assertEqual(summary["by_user"], {
            "private-alice": {
                "calls": 2, "success_rate": 0.5, "timeouts": 1, "errors": 0,
                "p50_ms": 100, "p95_ms": 100, "tokens_in": 101, "tokens_out": 0,
            },
            "private-bob": {
                "calls": 1, "success_rate": 0, "timeouts": 1, "errors": 0,
                "p50_ms": None, "p95_ms": None, "tokens_in": 0, "tokens_out": 0,
            },
        })
        summary["by_user"] = {
            "user-1": summary["by_user"]["private-alice"],
            "user-2": summary["by_user"]["private-bob"],
        }
        self.assertEqual(report["summary"], summary)
        self.assertEqual(event_ids(report["alerts"]), [
            ("budget_exceeded", ["r1"]), ("timeout_streak", ["r1", "r2"]),
            ("recovered", ["r3"]),
        ])
        self.assertEqual(report["alerts"][0]["user"], "user-1")
        calls = {"r1": call(1, "timeout", user="user-1", tokens_in=101),
                 "r2": call(2, "timeout", user="user-2"), "r3": call(3, user="user-1")}
        for event in report["alerts"]:
            self.assertEqual(event["records"], [calls[request_id] for request_id in event["request_ids"]])

    def test_09_export_aliases_private_model_and_request_labels(self):
        path_model = "/Users/demo-owner/models/private"
        host_model = "inference.private.example"
        path_request = "/var/log/demo-owner/requests"
        host_request = "worker.private.example"
        first = call(1, "timeout", path_model, tokens_in=11)
        second = call(2, "timeout", path_model)
        first["request_id"] = host_request
        second["request_id"] = path_request
        self.write_rows(first, second, call(3, model=host_model, user="demo-owner"), call(4))
        output = self.directory / "export.json"
        self.cli("export", "--config", self.config, "--out", output)

        payload = output.read_text()
        self.cli("export", "--config", self.config, "--out", output)
        self.assertEqual(output.read_text(), payload)
        for private in (path_model, host_model, path_request, host_request, "demo-owner"):
            self.assertNotIn(private, payload)
        report = json.loads(payload)
        budget, streak = report["alerts"]
        self.assertEqual([budget["type"], streak["type"]], ["budget_exceeded", "timeout_streak"])
        self.assertEqual(budget["request_ids"], streak["request_ids"][:1])
        self.assertEqual(budget["records"], streak["records"][:1])
        self.assertEqual(len(set(streak["request_ids"])), 2)
        self.assertEqual([record["request_id"] for record in streak["records"]], streak["request_ids"])
        self.assertEqual([record["model"] for record in streak["records"]], [streak["model"]] * 2)
        models = report["summary"]["by_model"]
        self.assertEqual(len(models), 3)
        self.assertEqual(models[streak["model"]]["timeouts"], 2)
        self.assertEqual(models["model-a"]["calls"], 1)

    def test_10_state_keeps_numbers_private_and_preserves_trace(self):
        first = call(1, "timeout", user="0912345678", tokens_in=60)
        first["request_id"] = "0987654321"
        self.write_rows(first)
        self.assertEqual(self.alerts().stdout, "")
        for private in (first["user"], first["request_id"]):
            self.assertNotIn(private, self.state.read_text())

        second = call(2, "timeout", user=first["user"], tokens_out=41)
        self.write_rows(second, append=True)
        streak, budget = events_from(self.alerts().stdout)
        self.assertEqual(event_ids([streak, budget]), [
            ("timeout_streak", [first["request_id"], "r2"]), ("budget_exceeded", ["r2"]),
        ])
        self.assertEqual(budget["tokens"], 101)
        self.assertEqual(self.cli("trace", streak["request_ids"][0]).stdout, json.dumps(first) + "\n")
        for private in (first["user"], first["request_id"]):
            self.assertNotIn(private, self.state.read_text())

    def test_11_alerts_retry_incomplete_last_line(self):
        self.write_rows(call(1, "timeout"))
        tail = json.dumps(call(2, "timeout", ts="2026-11-27T02:00:01Z"))
        with self.log.open("a", encoding="utf-8") as target:
            target.write(tail[:-1])
        pending = self.alerts()
        self.assertEqual(json.loads(self.state.read_text())["processed_lines"], 1)
        self.assertEqual((pending.stdout, pending.stderr), ("", ""))

        with self.log.open("a", encoding="utf-8") as target:
            target.write(tail[-1:] + "\nnot json\n")
        completed = self.alerts()
        self.assertEqual(event_ids(events_from(completed.stdout)), [("timeout_streak", ["r1", "r2"])])
        self.assertIn("line 3:", completed.stderr)
        self.assertEqual(json.loads(self.state.read_text())["processed_lines"], 3)
        self.assertEqual(self.alerts().stderr, "")

    def test_12_alerts_reject_older_appends_without_advancing(self):
        self.write_rows(call(2))
        self.assertEqual(self.alerts().stdout, "")
        previous_state = self.state.read_bytes()
        self.write_rows(call(1, "timeout"), call(3, "timeout"), append=True)
        full_run = self.cli("alerts", "--config", self.config, "--state", self.directory / "fresh.json")
        self.assertEqual(full_run.stdout, "")

        incremental = self.alerts(exit_code=1)
        self.assertEqual(incremental.stdout, "")
        self.assertIn("line 2:", incremental.stderr)
        self.assertIn("older than", incremental.stderr)
        self.assertEqual(self.state.read_bytes(), previous_state)

    def test_13_duplicate_request_ids_report_conflicting_lines(self):
        self.write_rows(call(1), "bad")
        self.assertEqual(self.alerts().stdout, "")
        previous_state = self.state.read_bytes()
        duplicate = call(2, "timeout")
        duplicate["request_id"] = "r1"
        self.write_rows(duplicate, call(3, "timeout"), append=True)
        output = self.directory / "export.json"
        for command, args in (
            ("trace", ("r1",)),
            ("export", ("--config", self.config, "--out", output)),
            ("alerts", ("--config", self.config, "--state", self.state)),
        ):
            with self.subTest(command=command):
                result = self.cli(command, *args, exit_code=1)
                self.assertEqual(result.stdout, "")
                self.assertIn("duplicate request_id 'r1' on lines 1 and 3", result.stderr)
        self.assertFalse(output.exists())
        self.assertEqual(self.state.read_bytes(), previous_state)

    def test_14_export_cannot_overwrite_its_inputs(self):
        self.write_rows(call(1))
        for source in (self.log, self.config):
            before = source.read_bytes()
            symlink = self.directory / f"{source.name}.link"
            symlink.symlink_to(source)
            hardlink = self.directory / f"{source.name}.hard"
            hardlink.hardlink_to(source)
            for output in (source, symlink, hardlink):
                with self.subTest(source=source.name, output=output.name):
                    result = subprocess.run(
                        [sys.executable, "-m", "agentwatch", "export", "--log", str(self.log),
                         "--config", str(self.config), "--out", str(output)],
                        capture_output=True, text=True,
                    )
                    after = source.read_bytes()
                    if after != before:
                        source.write_bytes(before)
                    self.assertEqual(after, before)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("--out must differ from", result.stderr)

    def test_15_configurable_streak_and_success_are_model_specific(self):
        config = json.loads(self.config.read_text())
        config["timeout_streak"] = 3
        self.config.write_text(json.dumps(config))
        self.write_rows(call(1, "timeout", "model-b"), call(2, "timeout"))
        self.assertEqual(self.alerts().stdout, "")
        self.write_rows(call(3), call(4, "timeout", "model-b"), append=True)
        self.assertEqual(self.alerts().stdout, "")
        self.write_rows(call(5, "timeout", "model-b"), append=True)
        events = events_from(self.alerts().stdout)
        self.assertEqual(event_ids(events), [("timeout_streak", ["r1", "r4", "r5"])])
        self.assertEqual(events[0]["model"], "model-b")

    def test_16_budget_uses_each_timestamp_local_date(self):
        self.write_rows(
            call(1, ts="2026-11-27T23:59:58-05:00", tokens_in=10),
            call(2, ts="2026-11-27T23:59:59-05:00", tokens_out=1),
            call(3, ts="2026-11-28T00:00:00-05:00", tokens_in=10),
            call(4, ts="2026-11-28T00:00:01-05:00", tokens_out=1),
        )
        self.assertEqual(
            [(event["date"], event["tokens"], event["request_ids"])
             for event in events_from(self.alerts().stdout)],
            [("2026-11-27", 11, ["r2"]), ("2026-11-28", 11, ["r4"])],
        )

    def test_17_budget_crosses_threshold_only_after_append(self):
        self.write_rows(call(1, tokens_in=6))
        self.assertEqual(self.alerts().stdout, "")
        self.write_rows(call(2, tokens_out=5), append=True)
        events = events_from(self.alerts().stdout)
        self.assertEqual(event_ids(events), [("budget_exceeded", ["r2"])])
        event = events[0]
        self.assertEqual((event["tokens"], event["budget"]), (11, 10))
        self.assertEqual(self.alerts().stdout, "")

    def test_18_nearest_rank_rounds_up_for_three_successes(self):
        self.write_rows(call(1, latency=30), call(2, latency=10), call(3, latency=20))
        metrics = json.loads(self.cli("summary").stdout)["by_user"]["alice"]
        self.assertEqual((metrics["p50_ms"], metrics["p95_ms"]), (20, 30))

    def test_19_legacy_state_keeps_progress_and_rewrites_identifiers(self):
        first = call(1, "timeout", user="0912345678", tokens_in=101)
        first["request_id"] = "0987654321"
        self.write_rows(first, call(2, "timeout", user=first["user"], tokens_out=1))
        self.state.write_text(json.dumps({
            "processed_lines": 1,
            "timeouts": {"model-a": [first["request_id"]]},
            "daily_usage": {first["user"]: {"2026-11-27": {"tokens": 101, "alerted": True}}},
        }))
        self.assertEqual(event_ids(events_from(self.alerts().stdout)), [
            ("timeout_streak", [first["request_id"], "r2"]),
        ])
        self.assertEqual(json.loads(self.state.read_text())["processed_lines"], 2)
        for private in (first["user"], first["request_id"]):
            self.assertNotIn(private, self.state.read_text())
