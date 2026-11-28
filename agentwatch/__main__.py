"""Command-line entry point for agent-watch."""

import argparse
import errno
import json
import os
import re
import stat
import sys
from datetime import date
from decimal import Decimal
from hashlib import sha256
from itertools import count
from pathlib import Path

from .alerts import atomic_output, new_state, process_records, read_config, read_state, upgrade_state
from .logread import Log, read_log
from .report import daily_comparison, write_report
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
    report = commands.add_parser("report", help="write a Chinese Markdown daily report")

    for command in (summary, alerts, trace, export, report):
        command.add_argument("--log", type=Path, required=True)
    summary.add_argument("--config", type=Path)
    for command in (alerts, export, report):
        command.add_argument("--config", type=Path, required=True)
    alerts.add_argument("--state", type=Path, required=True)
    trace.add_argument("request_id")
    sample.add_argument("--seed", type=int, required=True)
    sample.add_argument("--out", type=Path, required=True)
    export.add_argument("--out", type=Path, required=True)
    report.add_argument("--date", type=_report_date, required=True)
    report.add_argument("--out", type=Path, required=True)
    return parser


def _report_date(value: str) -> date:
    try:
        day = date.fromisoformat(value)
        if day.isoformat() == value:
            return day
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("--date must be YYYY-MM-DD")


def _export_aliases(
    values: set[str], prefix: str, private_values: set[str], public_values: set[str],
) -> dict[str, str]:
    aliases = {}
    numbers = count(1)
    for value in sorted(values):
        if value in public_values and value not in private_values:
            aliases[value] = value
            continue
        alias = f"{prefix}-{next(numbers)}"
        while alias in private_values:
            alias = f"{prefix}-{next(numbers)}"
        aliases[value] = alias
    return aliases


def _json_dumps(value: object, *, ensure_ascii: bool = True, indent: int | None = None) -> str:
    # The standard encoder handles strings and other scalars. Decimal needs an exact JSON number.
    def encode(item: object, depth: int) -> str:
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, dict):
            keys = sorted(item) if indent is not None else item
            entries = [f"{encode(key, depth + 1)}: {encode(item[key], depth + 1)}" for key in keys]
            return join(entries, "{", "}", depth)
        if isinstance(item, list):
            return join([encode(entry, depth + 1) for entry in item], "[", "]", depth)
        return json.dumps(item, ensure_ascii=ensure_ascii)

    def join(entries: list[str], opening: str, closing: str, depth: int) -> str:
        if indent is None or not entries:
            return opening + ", ".join(entries) + closing
        padding = " " * (indent * depth)
        next_line = "\n" + padding + " " * indent
        return opening + next_line + ("," + next_line).join(entries) + "\n" + padding + closing

    return encode(value, 0)


def _write_export(log: Log, log_path: Path, config_path: Path, output: Path) -> None:
    by_id = log.index_requests()
    users = {record.call["user"] for record in log.records}
    models = {record.call["model"] for record in log.records}
    requests = set(by_id)
    public_models = {model for model in models if re.fullmatch(r"model-[ab]", model)}
    public_requests = {request for request in requests if re.fullmatch(r"r[0-9]{1,4}", request)}
    private_values = users | (models - public_models) | (requests - public_requests)
    user_aliases = _export_aliases(users, "user", private_values, set())
    model_aliases = _export_aliases(models, "model", private_values, public_models)
    request_aliases = _export_aliases(requests, "request", private_values, public_requests)

    config = read_config(config_path)
    events = process_records(log.records, config, new_state())
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

    payload = {
        "summary": summarize(log, user_aliases, model_aliases, prices=config["prices"]),
        "alerts": events,
        "report": None,
    }
    if log.records:
        latest_day = max(record.timestamp.date() for record in log.records)
        payload["report"], _ = daily_comparison(log.records, latest_day, config["prices"] or {})
    _check_output(output, log_path, config_path)
    destination = output.resolve()
    if destination != output:
        _check_output(destination, log_path, config_path)
    try:
        mode = destination.stat().st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None and not stat.S_ISREG(mode):
        report = destination.open("w", encoding="utf-8", newline="\n")
    else:
        report = atomic_output(destination, creation_mode=0o666,
                               final_mode=stat.S_IMODE(mode) if mode is not None else None)
    with report as target:
        print(_json_dumps(payload, indent=2), file=target)


def _check_output(output: Path, log_path: Path, config_path: Path) -> None:
    for option, source in (("--log", log_path), ("--config", config_path)):
        try:
            same_file = output.samefile(source)
        except FileNotFoundError:
            continue
        if same_file:
            raise ValueError(f"--out must differ from {option}")


def _alert_lock_path(state: Path) -> Path:
    lock_path = state.with_name(state.name + ".lock")
    try:
        lock_path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        if exc.errno != errno.ENAMETOOLONG:
            raise
        name_max = os.pathconf(state.parent, "PC_NAME_MAX")
        digest = sha256(os.fsencode(state.name)).hexdigest()
        if name_max > 0:
            digest = digest[:name_max - len("..lock")]
        return state.with_name(f".{digest}.lock")
    return lock_path


def main() -> int:
    args = _parser().parse_args()
    state_lock = None
    try:
        if args.command == "sample":
            write_sample(args.out, args.seed)
            return 0

        is_alerts = args.command == "alerts"
        if is_alerts:
            try:
                import fcntl
            except ImportError as exc:
                raise ValueError("alerts requires Unix file locking (fcntl)") from exc

            # The state file is replaced on save, so lock a separate, stable file.
            lock_path = _alert_lock_path(args.state)
            for option, source in (("--log", args.log), ("--config", args.config)):
                try:
                    same_file = source.samefile(lock_path)
                except FileNotFoundError:
                    same_file = source.resolve() == lock_path.resolve()
                    if not same_file:
                        source.stat()  # Reject missing inputs before creating the lock.
                if same_file:
                    raise ValueError(f"alerts lock file must differ from {option}")
            try:
                state_lock = lock_path.open("a")
            except PermissionError:
                state_lock = lock_path.open("r")
            fcntl.flock(state_lock, fcntl.LOCK_EX)
        state = read_state(args.state) if is_alerts else new_state()
        log = read_log(args.log, defer_partial_tail=is_alerts, processed_lines=state["processed_lines"])
        for line_number, reason in log.bad_lines:
            if line_number > state["processed_lines"]:
                print(f"line {line_number}: {reason}", file=sys.stderr)

        if args.command == "summary":
            log.index_requests()
            prices = read_config(args.config)["prices"] if args.config is not None else None
            print(_json_dumps(summarize(log, prices=prices), ensure_ascii=prices is None))
        elif args.command == "alerts":
            if log.line_count < state["processed_lines"]:
                raise ValueError("log is shorter than saved alert progress")
            log.index_requests()
            config = read_config(args.config)
            state = upgrade_state(state, log.records, config)
            events = process_records(log.records, config, state)
            state["processed_lines"] = log.line_count
            with atomic_output(args.state) as target:
                json.dump(state, target)
                # Closing surfaces buffered write errors before emitting any alerts.
                target.close()
                for event in events:
                    print(json.dumps(event))
                sys.stdout.flush()
        elif args.command == "trace":
            record = log.index_requests().get(args.request_id)
            if record is not None:
                print(record.raw)
                return 0
            print(f"request_id not found: {args.request_id}", file=sys.stderr)
            return 1
        elif args.command == "export":
            _write_export(log, args.log, args.config, args.out)
        elif args.command == "report":
            log.index_requests()
            _check_output(args.out, args.log, args.config)
            write_report(log.records, args.date, read_config(args.config), args.out,
                         bad_line_count=len(log.bad_lines))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if state_lock is not None:
            state_lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
