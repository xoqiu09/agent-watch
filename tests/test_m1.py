import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from hashlib import sha256
from io import StringIO
from pathlib import Path
from select import select
from unittest.mock import patch

from agentwatch.__main__ import main

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

    def cli(self, command, *args, exit_code=0, umask=-1):
        result = subprocess.run(
            [sys.executable, "-m", "agentwatch", command, "--log", str(self.log), *map(str, args)],
            capture_output=True, text=True, umask=umask,
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

    def test_concurrent_alerts_do_not_rewind_state_or_repeat_events(self):
        import fcntl

        self.write_rows(call(1, "timeout", tokens_in=11))
        args = ["alerts", "--log", str(self.log), "--config", str(self.config),
                "--state", str(self.state)]
        pause_before_save = """
import sys
from contextlib import contextmanager
from agentwatch import __main__ as cli

atomic_output = cli.atomic_output

@contextmanager
def paused_output(path):
    print("ready to save", file=sys.stderr, flush=True)
    sys.stdin.read()
    with atomic_output(path) as target:
        yield target

cli.atomic_output = paused_output
raise SystemExit(cli.main())
"""
        flock = fcntl.flock
        first_output = None
        second_stdout = StringIO()
        with subprocess.Popen(
            [sys.executable, "-c", pause_before_save, *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as first:
            try:
                self.assertTrue(select([first.stderr], [], [], 10)[0], "first run did not reach save")
                self.assertEqual(first.stderr.readline(), "ready to save\n")
                self.write_rows(call(2, "timeout"), append=True)

                def finish_first_if_blocked(file, operation):
                    nonlocal first_output
                    try:
                        flock(file, operation | fcntl.LOCK_NB)
                    except BlockingIOError:
                        first_output = first.communicate(timeout=10)
                        flock(file, operation)

                # Let the first run save when the second would have to wait for it.
                with (
                    patch.object(fcntl, "flock", finish_first_if_blocked),
                    patch("sys.argv", ["agentwatch", *args]),
                    redirect_stdout(second_stdout),
                ):
                    self.assertEqual(main(), 0)
                if first_output is None:
                    first_output = first.communicate(timeout=10)
                self.assertEqual(first.returncode, 0, first_output[1])
            finally:
                first.kill()

        processed_lines = json.loads(self.state.read_text())["processed_lines"]
        initial_events = event_ids(events_from(first_output[0] + second_stdout.getvalue()))
        replay_events = event_ids(events_from(self.alerts().stdout))
        self.assertEqual((processed_lines, initial_events, replay_events),
                         (2, [("budget_exceeded", ["r1"]), ("timeout_streak", ["r1", "r2"])], []))

    def test_alerts_does_not_create_a_missing_log_at_the_lock_path(self):
        self.log = self.state.with_name("state.json.lock")

        result = self.alerts(exit_code=1)

        self.assertIn("alerts lock file must differ from --log", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.log.exists())
        self.assertFalse(self.state.exists())

    def test_alerts_rejects_a_lock_file_linked_to_the_config(self):
        self.write_rows(call(1))
        lock_path = self.state.with_name("state.json.lock")
        lock_path.hardlink_to(self.config)
        original_config = self.config.read_bytes()

        result = self.alerts(exit_code=1)

        self.assertIn("alerts lock file must differ from --config", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.config.read_bytes(), original_config)
        self.assertTrue(lock_path.samefile(self.config))
        self.assertFalse(self.state.exists())

    def test_alerts_explains_when_unix_file_locking_is_unavailable(self):
        self.write_rows(call(1))
        argv = ["agentwatch", "alerts", "--log", str(self.log), "--config", str(self.config),
                "--state", str(self.state)]
        stderr = StringIO()
        with (
            patch.dict(sys.modules, {"fcntl": None}),
            patch.object(sys, "argv", argv),
            redirect_stderr(stderr),
        ):
            self.assertEqual(main(), 1)
        self.assertEqual(stderr.getvalue(), "error: alerts requires Unix file locking (fcntl)\n")
        self.assertFalse(self.state.with_name("state.json.lock").exists())
        self.assertFalse(self.state.exists())

    def test_alerts_reuse_a_read_only_lock(self):
        if os.geteuid() == 0:
            self.skipTest("root can open a read-only file for writing")
        self.write_rows(call(1, tokens_in=11))
        args = ("--config", self.config, "--state", self.state)

        first = self.cli("alerts", *args, umask=0o222)

        self.assertEqual(event_ids(events_from(first.stdout)), [("budget_exceeded", ["r1"])])
        lock_path = self.state.with_name("state.json.lock")
        self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o444)
        second = self.cli("alerts", *args, umask=0o222)
        self.assertEqual((second.stdout, second.stderr), ("", ""))

    def test_alerts_reuse_a_lock_for_a_long_state_name(self):
        name_max = os.pathconf(self.directory, "PC_NAME_MAX")
        if name_max == -1:
            self.skipTest("filesystem does not report a filename limit")
        self.state = self.directory / ("s" * (name_max - 4))
        self.write_rows(call(1, tokens_in=11))
        inputs = set(self.directory.iterdir())

        first = self.alerts()

        self.assertEqual(event_ids(events_from(first.stdout)), [("budget_exceeded", ["r1"])])
        self.assertEqual(json.loads(self.state.read_text())["processed_lines"], 1)
        new_files = set(self.directory.iterdir()) - inputs
        self.assertEqual(len(new_files - {self.state}), 1)
        second = self.alerts()
        self.assertEqual((second.stdout, second.stderr), ("", ""))
        self.assertEqual(set(self.directory.iterdir()) - inputs, new_files)

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

    def test_20_alerts_keep_submicrosecond_order_and_progress(self):
        with self.subTest("sort within a microsecond"):
            self.write_rows(
                call(1, "timeout", ts="2026-11-27T02:00:00.000000900Z"),
                call(2, ts="2026-11-27T10:00:00.000000100+08:00"),
                call(3, "timeout", ts="2026-11-27T02:00:00.000001000Z"),
            )
            self.assertEqual(event_ids(events_from(self.alerts().stdout)), [
                ("timeout_streak", ["r1", "r3"]),
            ])

        with self.subTest("reject an older append within a microsecond"):
            self.state = self.directory / "incremental.json"
            self.write_rows(call(1, "timeout", ts="2026-11-27T02:00:00.000000900Z"))
            self.assertEqual(self.alerts().stdout, "")
            previous_state = self.state.read_bytes()
            self.write_rows(call(2, "timeout", ts="2026-11-27T10:00:00.000000100+08:00"),
                            append=True)
            rejected = self.alerts(exit_code=1)
            self.assertEqual(rejected.stdout, "")
            self.assertIn("line 2: timestamp is older", rejected.stderr)
            self.assertEqual(self.state.read_bytes(), previous_state)

    def test_21_alerts_do_not_emit_until_state_can_be_written(self):
        root = Path(__file__).resolve().parents[1]
        self.log = root / "data" / "sample.jsonl"
        self.config = root / "config.json"
        self.state = self.directory / "missing" / "state.json"

        failed = self.alerts(exit_code=1)
        self.assertIn("error:", failed.stderr)
        self.assertEqual(len(events_from(failed.stdout)), 0)

        self.state.parent.mkdir()
        retried = events_from(self.alerts().stdout)
        self.assertEqual(len(retried), 8)
        self.assertEqual(len({(kind, tuple(ids)) for kind, ids in event_ids(retried)}), 8)
        self.assertEqual(self.alerts().stdout, "")

    def test_alerts_do_not_emit_when_state_write_fails_after_locking(self):
        import fcntl

        self.write_rows(call(1, tokens_in=11))
        argv = ["agentwatch", "alerts", "--log", str(self.log), "--config", str(self.config),
                "--state", str(self.state)]
        stdout, stderr = StringIO(), StringIO()
        with (
            patch.object(sys, "argv", argv),
            patch.object(fcntl, "flock", wraps=fcntl.flock) as lock,
            patch("agentwatch.__main__.json.dump",
                  side_effect=OSError("simulated state write failure")) as write,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(main(), 1)

        lock.assert_called_once()
        write.assert_called_once()
        self.assertEqual(stderr.getvalue(), "error: simulated state write failure\n")
        self.assertEqual(events_from(stdout.getvalue()), [])
        self.assertFalse(self.state.exists())
        self.assertEqual(event_ids(events_from(self.alerts().stdout)), [("budget_exceeded", ["r1"])])

    def test_22_export_aliases_cannot_reuse_private_identifiers(self):
        model = "model-1"
        requests = ["request-1", "request-2"]
        first = call(1, "timeout", model)
        second = call(2, "timeout", model)
        first["request_id"], second["request_id"] = requests
        self.write_rows(first, second)
        output = self.directory / "export.json"
        self.cli("export", "--config", self.config, "--out", output)

        report = json.loads(output.read_text())
        event, = report["alerts"]
        models = report["summary"]["by_model"]
        self.assertTrue({model, *requests}.isdisjoint({*models, *event["request_ids"]}),
                        (list(models), event["request_ids"]))
        self.assertEqual(list(models), [event["model"]])
        self.assertEqual(len(set(event["request_ids"])), 2)
        self.assertEqual([record["request_id"] for record in event["records"]],
                         event["request_ids"])
        self.assertEqual([record["model"] for record in event["records"]], [event["model"]] * 2)

    def test_23_failed_export_preserves_the_previous_report(self):
        self.write_rows(call(1))
        output = self.directory / "export.json"
        self.cli("export", "--config", self.config, "--out", output)
        previous_report = output.read_bytes()
        previous_files = set(self.directory.iterdir())
        self.write_rows(call(2), append=True)

        stderr = StringIO()

        def fail_report_write(value, *, file):
            if file is stderr:
                print(value, file=file)
                return
            file.write(value[:10])
            raise OSError("simulated disk full")

        argv = ["agentwatch", "export", "--log", str(self.log), "--config", str(self.config),
                "--out", str(output)]
        with (
            patch.object(sys, "argv", argv),
            patch("agentwatch.__main__.print", fail_report_write, create=True),
            redirect_stderr(stderr),
        ):
            self.assertEqual(main(), 1)
        self.assertIn("simulated disk full", stderr.getvalue())
        self.assertEqual(output.read_bytes(), previous_report)
        self.assertEqual(set(self.directory.iterdir()), previous_files)

    def test_24_alerts_report_an_invalid_state_cursor(self):
        self.write_rows(call(1))
        for state, reason in (
            ({}, "missing processed_lines"),
            ({"processed_lines": -1}, "processed_lines must be a non-negative integer"),
            ({"processed_lines": True}, "processed_lines must be a non-negative integer"),
            ([], "must contain a JSON object"),
        ):
            with self.subTest(state=state):
                original = json.dumps(state).encode()
                self.state.write_bytes(original)
                result = self.alerts(exit_code=1)
                self.assertEqual(result.stdout, "")
                self.assertIn(reason, result.stderr)
                self.assertIn(str(self.state), result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(self.state.read_bytes(), original)

    def test_25_alerts_rebuild_truncated_legacy_progress(self):
        first = call(1, "timeout", ts="2026-11-27T02:00:00.000000900Z")
        second = call(2, ts="2026-11-27T02:00:00.000000100Z")
        # The old version processed these two calls in file order, clearing the streak.
        legacy_state = json.dumps({
            "processed_lines": 2,
            "last_timestamp": "2026-11-27T02:00:00+00:00",
            "timeouts": {},
            "daily_usage": {sha256(b"alice").hexdigest(): {
                "2026-11-27": {"tokens": 0, "alerted": False},
            }},
        }).encode()
        for fraction, exit_code in (("000000500", 1), ("000001000", 0)):
            with self.subTest(fraction=fraction):
                self.state.write_bytes(legacy_state)
                self.write_rows(first, second, call(3, "timeout", ts=f"2026-11-27T02:00:00.{fraction}Z"))
                result = self.alerts(exit_code=exit_code)
                if exit_code:
                    self.assertEqual(result.stdout, "")
                    self.assertIn("line 3: timestamp is older", result.stderr)
                    self.assertEqual(self.state.read_bytes(), legacy_state)
                else:
                    self.assertEqual(event_ids(events_from(result.stdout)), [
                        ("timeout_streak", ["r1", "r3"]),
                    ])
                    self.assertEqual(self.alerts().stdout, "")

    def test_26_alerts_sort_by_utc_with_fractional_offsets(self):
        self.write_rows(
            call(1, "timeout", ts="2026-11-27T02:00:00.000000500Z"),
            call(2, ts="2026-11-27T03:00:00.000000900+01:00:00.000000800"),
            call(3, "timeout", ts="2026-11-27T03:00:00.000001800+01:00:00.000000800"),
        )
        result = self.alerts()
        self.assertEqual(result.stderr, "")
        self.assertEqual(event_ids(events_from(result.stdout)), [
            ("timeout_streak", ["r1", "r3"]),
        ])
        previous_state = self.state.read_bytes()
        self.write_rows(call(4, ts="2026-11-27T02:00:00.000000999Z"), append=True)
        result = self.alerts(exit_code=1)
        self.assertEqual(result.stdout, "")
        self.assertIn("line 4: timestamp is older", result.stderr)
        self.assertEqual(self.state.read_bytes(), previous_state)

    @unittest.skipUnless(os.name == "posix", "requires POSIX file modes and umask")
    def test_27_export_respects_file_permissions(self):
        self.write_rows(call(1))
        output = self.directory / "export.json"
        with self.subTest("new report uses the umask"):
            self.cli("export", "--config", self.config, "--out", output, umask=0o027)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o640)
        with self.subTest("overwrite keeps the existing mode"):
            output.chmod(0o644)
            self.cli("export", "--config", self.config, "--out", output, umask=0o077)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o644)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX named pipes")
    def test_28_export_streams_to_a_named_pipe(self):
        self.write_rows(call(1))
        output = self.directory / "export.pipe"
        os.mkfifo(output)
        reader = os.open(output, os.O_RDONLY | os.O_NONBLOCK)
        try:
            self.cli("export", "--config", self.config, "--out", output)
            self.assertTrue(stat.S_ISFIFO(output.stat().st_mode))
            report = json.loads(os.read(reader, 4096))
            self.assertEqual(report["alerts"], [])
            self.assertEqual(report["summary"]["by_user"]["user-1"]["calls"], 1)
        finally:
            os.close(reader)

    def test_29_export_rejects_resolved_input_paths(self):
        self.write_rows(call(1))
        original_log = self.log.read_bytes()
        original_config = self.config.read_bytes()
        for option, source in (("--log", self.log), ("--config", self.config)):
            linked = self.directory / f"linked-{source.name}"
            linked.hardlink_to(source)
            unresolved = self.directory / "missing" / ".." / source.name
            for output in (linked, unresolved):
                with self.subTest(output=output):
                    self.log.write_bytes(original_log)
                    self.config.write_bytes(original_config)
                    result = self.cli("export", "--config", self.config, "--out", output,
                                      exit_code=1)
                    self.assertIn(f"--out must differ from {option}", result.stderr)
                    self.assertEqual(self.log.read_bytes(), original_log)
                    self.assertEqual(self.config.read_bytes(), original_config)

    def test_30_alerts_keep_fraction_before_a_spaced_time_zone(self):
        first = call(1, "timeout", ts="2026-11-27T02:00:00.900000000 +00:00")
        older = call(2, ts="2026-11-27T02:00:00.100000000 +00:00")
        next_timeout = call(3, "timeout", ts="2026-11-27T02:00:01.000000000 +00:00")
        try:
            datetime.fromisoformat(first["ts"])
        except ValueError:
            self.skipTest("datetime.fromisoformat does not accept a space before the time zone")

        with self.subTest("sort fractions before the time zone"):
            self.write_rows(first, older, next_timeout)
            result = self.alerts()
            self.assertEqual(result.stderr, "")
            self.assertEqual(event_ids(events_from(result.stdout)), [
                ("timeout_streak", ["r1", "r3"]),
            ])

        with self.subTest("reject an older append within the same microsecond"):
            self.state = self.directory / "incremental.json"
            first["ts"] = "2026-11-27T02:00:00.000000900 +00:00"
            older["ts"] = "2026-11-27T02:00:00.000000100 +00:00"
            self.write_rows(first)
            self.assertEqual(self.alerts().stdout, "")
            previous_state = self.state.read_bytes()
            self.write_rows(older, append=True)
            result = self.alerts(exit_code=1)
            self.assertEqual(result.stdout, "")
            self.assertIn("line 2: timestamp is older", result.stderr)
            self.assertEqual(self.state.read_bytes(), previous_state)
