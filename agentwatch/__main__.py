"""Command-line entry point for agent-watch."""

import argparse
import json
import re
import sys
from itertools import count
from pathlib import Path

from .alerts import new_state, process_records, read_config, read_state, upgrade_state, write_state
from .logread import Log, read_log
from .sample import write_sample
from .summary import summarize


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m agentwatch")
    commands = parser.add_subparsers(dest="command", required=True)
    summary = commands.add_parser("summary", help="summarize calls by user and model")
    alerts = commands.add_parser("alerts", help="evaluate alerts for new log lines")
    trace = commands.add_parser("trace", help="retrieve an original call record")
    sample = commands.add_parser("sample", help="generate a synthetic call log")
    export = commands.add_parser("export", help="write a deterministic demo JSON")

    for command in (summary, alerts, trace, export):
        command.add_argument("--log", type=Path, required=True)
    for command in (alerts, export):
        command.add_argument("--config", type=Path, required=True)
    alerts.add_argument("--state", type=Path, required=True)
    trace.add_argument("request_id")
    sample.add_argument("--seed", type=int, required=True)
    sample.add_argument("--out", type=Path, required=True)
    export.add_argument("--out", type=Path, required=True)
    return parser


def _export_aliases(
    values: set[str], prefix: str, users: set[str], public_pattern: str | None = None,
) -> dict[str, str]:
    aliases = {}
    numbers = count(1)
    for value in sorted(values):
        if public_pattern is not None and value not in users and re.fullmatch(public_pattern, value):
            aliases[value] = value
            continue
        alias = f"{prefix}-{next(numbers)}"
        while alias in users:
            alias = f"{prefix}-{next(numbers)}"
        aliases[value] = alias
    return aliases


def _write_export(log: Log, log_path: Path, config_path: Path, output: Path) -> None:
    by_id = log.index_requests()
    users = {record.call["user"] for record in log.records}
    user_aliases = _export_aliases(users, "user", users)
    model_aliases = _export_aliases(
        {record.call["model"] for record in log.records}, "model", users, r"model-[ab]",
    )
    request_aliases = _export_aliases(set(by_id), "request", users, r"r[0-9]{1,4}")

    events = process_records(log.records, read_config(config_path), new_state())
    for event in events:
        if "user" in event:
            event["user"] = user_aliases[event["user"]]
        if "model" in event:
            event["model"] = model_aliases[event["model"]]
        event["records"] = []
        for request_id in event["request_ids"]:
            call = by_id[request_id].call
            event["records"].append({
                **call,
                "request_id": request_aliases[request_id],
                "user": user_aliases[call["user"]],
                "model": model_aliases[call["model"]],
            })
        event["request_ids"] = [request_aliases[request_id] for request_id in event["request_ids"]]

    payload = {"summary": summarize(log, user_aliases, model_aliases), "alerts": events}
    for option, source in (("--log", log_path), ("--config", config_path)):
        try:
            same_file = output.samefile(source)
        except FileNotFoundError:
            continue
        if same_file:
            raise ValueError(f"--out must differ from {option}")
    with output.open("w", encoding="utf-8", newline="\n") as target:
        print(json.dumps(payload, sort_keys=True, indent=2), file=target)


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "sample":
            write_sample(args.out, args.seed)
            return 0

        is_alerts = args.command == "alerts"
        state = read_state(args.state) if is_alerts else new_state()
        log = read_log(args.log, defer_partial_tail=is_alerts)
        for line_number, reason in log.bad_lines:
            if line_number > state["processed_lines"]:
                print(f"line {line_number}: {reason}", file=sys.stderr)

        if args.command == "summary":
            print(json.dumps(summarize(log)))
        elif args.command == "alerts":
            log.index_requests()
            config = read_config(args.config)
            state = upgrade_state(state, log.records, config)
            for event in process_records(log.records, config, state):
                print(json.dumps(event))
            sys.stdout.flush()
            state["processed_lines"] = log.line_count
            write_state(args.state, state)
        elif args.command == "trace":
            record = log.index_requests().get(args.request_id)
            if record is not None:
                print(record.raw)
                return 0
            print(f"request_id not found: {args.request_id}", file=sys.stderr)
            return 1
        elif args.command == "export":
            _write_export(log, args.log, args.config, args.out)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
