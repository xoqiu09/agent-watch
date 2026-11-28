"""Evaluate alert rules and persist the log position."""

import json
import os
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator, TextIO, TypedDict

from .logread import Record, parse_timestamp


LOG_VALIDATION_VERSION = 1


class Price(TypedDict):
    in_per_1k: Fraction
    out_per_1k: Fraction


Prices = dict[str, Price]


class Config(TypedDict):
    timeout_streak: int
    daily_token_budget: dict[str, int]
    default_daily_token_budget: int
    prices: Prices | None
    slow_call_ms: int | None


class DailyUsage(TypedDict):
    tokens: int
    alerted: bool


class State(TypedDict):
    processed_lines: int
    log_digest: str | None
    timeout_streak: int | None
    last_timestamp: str | None
    exact_timestamps: bool
    log_validation_version: int
    offset_minutes_validated: bool
    timeouts: dict[str, list[int]]
    alerted_timeouts: dict[str, bool]
    daily_usage: dict[str, dict[str, DailyUsage]]


def _parse_config_float(value: str) -> Decimal | None:
    try:
        return Decimal(value)
    except InvalidOperation:
        # Leave unrepresentable numbers for the field validators to reject.
        return None


class _ConfigObject(dict):
    def __init__(self, pairs: list[tuple[str, object]]):
        super().__init__()
        self.duplicate_key: str | None = None
        self.budget_sections: list[_ConfigObject] = []
        for key, value in pairs:
            if self.duplicate_key is None and key in self:
                self.duplicate_key = key
            if key == "daily_token_budget" and isinstance(value, _ConfigObject):
                self.budget_sections.append(value)
            self[key] = value


def read_config(path: Path) -> Config:
    config = json.loads(path.read_text(encoding="utf-8"), parse_float=_parse_config_float,
                        object_pairs_hook=_ConfigObject)
    if not isinstance(config, _ConfigObject):
        raise ValueError("config must be a JSON object")
    streak = config.get("timeout_streak")
    if type(streak) is not int or streak < 1:
        raise ValueError("timeout_streak must be a positive integer")
    seen_users: set[str] = set()
    for section in config.budget_sections:
        duplicate = section.duplicate_key
        if duplicate is None:
            duplicate = next((user for user in section if user in seen_users), None)
        if duplicate is not None:
            raise ValueError(f"duplicate daily_token_budget user {duplicate!r}")
        seen_users.update(section)

    budgets = config.get("daily_token_budget")
    if not isinstance(budgets, dict) or any(
        type(budget) is not int or budget < 0 for budget in budgets.values()
    ):
        raise ValueError("daily_token_budget must map users to non-negative integers")
    default = config.get("default_daily_token_budget")
    if type(default) is not int or default < 0:
        raise ValueError("default_daily_token_budget must be a non-negative integer")
    slow_call_ms = config.get("slow_call_ms")
    if "slow_call_ms" in config and (type(slow_call_ms) is not int or slow_call_ms < 1):
        raise ValueError("slow_call_ms must be a positive integer")
    return Config(
        timeout_streak=streak,
        daily_token_budget=budgets,
        default_daily_token_budget=default,
        prices=_parse_prices(config["prices"]) if "prices" in config else None,
        slow_call_ms=slow_call_ms,
    )


def _parse_prices(raw: object) -> Prices:
    if not isinstance(raw, dict):
        raise ValueError("prices must map models to input and output prices")
    price_fields = ("in_per_1k", "out_per_1k")
    prices: Prices = {}
    for model, price in raw.items():
        if not isinstance(price, dict) or any(
            type(price.get(field)) not in (int, Decimal) for field in price_fields
        ):
            raise ValueError(f"prices[{model!r}] must give numeric in_per_1k and out_per_1k")
        for field in price_fields:
            if price[field] < 0:
                raise ValueError(f"prices[{model!r}].{field} must be non-negative")
        prices[model] = Price(
            in_per_1k=Fraction(price["in_per_1k"]), out_per_1k=Fraction(price["out_per_1k"]),
        )
    return prices


def new_state() -> State:
    return State(
        processed_lines=0, log_digest=None, timeout_streak=None,
        last_timestamp=None, exact_timestamps=True,
        log_validation_version=LOG_VALIDATION_VERSION, offset_minutes_validated=True,
        timeouts={}, alerted_timeouts={}, daily_usage={},
    )


def read_state(path: Path) -> State:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return new_state()
    if not isinstance(state, dict):
        raise ValueError(f"state file {path} must contain a JSON object")
    if "processed_lines" not in state:
        raise ValueError(f"state file {path}: missing processed_lines")
    processed_lines = state["processed_lines"]
    if type(processed_lines) is not int or processed_lines < 0:
        raise ValueError(f"state file {path}: processed_lines must be a non-negative integer")
    return state


@contextmanager
def atomic_output(
    path: Path, *, creation_mode: int = 0o600, final_mode: int | None = None,
) -> Iterator[TextIO]:
    with TemporaryDirectory(dir=path.parent) as directory:
        staging = Path(directory)
        # Keep staging accessible to its owner even under a restrictive umask.
        staging.chmod(0o700)
        temporary_path = staging / "output"
        with open(temporary_path, "x", encoding="utf-8", newline="\n",
                  opener=lambda name, flags: os.open(name, flags, creation_mode)) as target:
            yield target
        if final_mode is not None:
            temporary_path.chmod(final_mode)
        temporary_path.replace(path)


def _state_key(value: str) -> str:
    return sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def _timeout_event(
    record: Record, state: State, threshold: int, requests_by_line: dict[int, str],
    *, replay: bool,
) -> dict | None:
    call = record.call
    if call["status"] == "error":
        return None
    model = call["model"]
    key = _state_key(model)
    if call["status"] == "ok":
        state["timeouts"].pop(key, None)
        if state["alerted_timeouts"].pop(key, False):
            return {
                "type": "recovered",
                "ts": call["ts"],
                "model": model,
                "request_ids": [call["request_id"]],
            }
        return None

    if key in state["alerted_timeouts"]:
        return None
    streak = state["timeouts"].setdefault(key, [])
    if len(streak) == threshold:
        del streak[0]
    streak.append(record.line_number)
    if len(streak) == threshold and not replay:
        state["alerted_timeouts"][key] = True
        return {
            "type": "timeout_streak",
            "ts": call["ts"],
            "model": model,
            "request_ids": [requests_by_line[line] for line in streak],
        }
    return None


def _budget_event(record: Record, state: State, config: Config, *, replay: bool) -> dict | None:
    call = record.call
    user = call["user"]
    day = record.timestamp.date().isoformat()
    days = state["daily_usage"].setdefault(_state_key(user), {})
    usage = days.setdefault(day, DailyUsage(tokens=0, alerted=False))
    usage["tokens"] += call["tokens_in"] + call["tokens_out"]
    budget = config["daily_token_budget"].get(user, config["default_daily_token_budget"])
    if not replay and usage["tokens"] > budget and not usage["alerted"]:
        usage["alerted"] = True
        return {
            "type": "budget_exceeded",
            "ts": call["ts"],
            "user": user,
            "date": day,
            "tokens": usage["tokens"],
            "budget": budget,
            "request_ids": [call["request_id"]],
        }
    return None


def process_records(
    records: list[Record], config: Config, state: State, *, slow_call_ms: int | None = None,
    replay: bool = False,
) -> list[dict]:
    ordered = sorted(
        (record for record in records if record.line_number > state["processed_lines"]),
        key=lambda record: record.utc_seconds,
    )
    if not ordered:
        return []
    latest = state["last_timestamp"]
    first = ordered[0]
    if latest is not None and first.utc_seconds < parse_timestamp(latest)[1]:
        raise ValueError(f"line {first.line_number}: timestamp is older than last processed time {latest}")

    requests_by_line = {record.line_number: record.call["request_id"] for record in records}
    events = []
    for record in ordered:
        timeout = _timeout_event(record, state, config["timeout_streak"], requests_by_line,
                                 replay=replay)
        if timeout is not None:
            events.append(timeout)
        budget = _budget_event(record, state, config, replay=replay)
        if budget is not None:
            events.append(budget)
        call = record.call
        if slow_call_ms is not None and call["status"] == "ok" and call["latency_ms"] > slow_call_ms:
            events.append({
                "type": "slow_call",
                "ts": call["ts"],
                "model": call["model"],
                "request_id": call["request_id"],
                "latency_ms": call["latency_ms"],
            })
    state["last_timestamp"] = ordered[-1].call["ts"]
    return events


def upgrade_state(state: State, records: list[Record], config: Config) -> State:
    raw_keys = "last_timestamp" not in state
    current_validation = (
        state.get("log_validation_version") == LOG_VALIDATION_VERSION
        and state.get("offset_minutes_validated") is True
    )
    processed_lines = state["processed_lines"]
    saved_timeouts = state.get("timeouts", {})
    if state.get("log_digest") is None and not raw_keys and saved_timeouts:
        calls_by_line = {
            record.line_number: record.call for record in records
            if record.line_number <= processed_lines
        }
        for model, streak in saved_timeouts.items():
            for line in streak:
                call = calls_by_line.get(line)
                # A tighter parser can drop an old row when rebuilding progress.
                if call is None and not current_validation and 0 < line <= processed_lines:
                    continue
                if call is None or call["status"] != "timeout" or _state_key(call["model"]) != model:
                    raise ValueError("log does not match saved alert progress")

    saved_streak = state.get("timeout_streak")
    if saved_streak is None and saved_timeouts:
        raise ValueError("cannot verify timeout_streak for legacy alert state with pending timeouts")
    if saved_streak is not None and saved_streak != config["timeout_streak"]:
        raise ValueError("timeout_streak changed since the alert state was saved")

    if "alerted_timeouts" not in state:
        state["alerted_timeouts"] = {
            (_state_key(model) if raw_keys else model): True
            for model, streak in saved_timeouts.items()
            if len(streak) == config["timeout_streak"]
        }
    if state.get("exact_timestamps") is True and current_validation:
        return state

    # Recompute counts with current validation and carry over only notices already sent.
    upgraded = new_state()
    process_records([record for record in records if record.line_number <= processed_lines],
                    config, upgraded, replay=True)
    upgraded["alerted_timeouts"] = state["alerted_timeouts"]
    for user, days in state.get("daily_usage", {}).items():
        for day, old_usage in days.items():
            if old_usage["alerted"]:
                key = _state_key(user) if raw_keys else user
                usage = upgraded["daily_usage"].setdefault(key, {}).setdefault(
                    day, DailyUsage(tokens=0, alerted=False),
                )
                usage["alerted"] = True
    upgraded["processed_lines"] = processed_lines
    return upgraded
