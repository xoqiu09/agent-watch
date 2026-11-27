"""Evaluate timeout and token budget rules and persist the log position."""

import json
import os
from contextlib import contextmanager
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator, TextIO, TypedDict

from .logread import Record, parse_timestamp


class Price(TypedDict):
    in_per_1k: Fraction
    out_per_1k: Fraction


Prices = dict[str, Price]


class Config(TypedDict):
    timeout_streak: int
    daily_token_budget: dict[str, int]
    default_daily_token_budget: int
    prices: Prices | None


class DailyUsage(TypedDict):
    tokens: int
    alerted: bool


class State(TypedDict):
    processed_lines: int
    last_timestamp: str | None
    exact_timestamps: bool
    timeouts: dict[str, list[int]]
    daily_usage: dict[str, dict[str, DailyUsage]]


def read_config(path: Path) -> Config:
    config = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    if not isinstance(config, dict):
        raise ValueError("config must be a JSON object")
    streak = config.get("timeout_streak")
    if type(streak) is not int or streak < 1:
        raise ValueError("timeout_streak must be a positive integer")
    budgets = config.get("daily_token_budget")
    if not isinstance(budgets, dict) or any(
        type(budget) is not int or budget < 0 for budget in budgets.values()
    ):
        raise ValueError("daily_token_budget must map users to non-negative integers")
    default = config.get("default_daily_token_budget")
    if type(default) is not int or default < 0:
        raise ValueError("default_daily_token_budget must be a non-negative integer")
    return Config(
        timeout_streak=streak,
        daily_token_budget=budgets,
        default_daily_token_budget=default,
        prices=_parse_prices(config["prices"]) if "prices" in config else None,
    )


def _parse_prices(raw: object) -> Prices:
    if not isinstance(raw, dict):
        raise ValueError("prices must map models to input and output prices")
    prices: Prices = {}
    for model, price in raw.items():
        if not isinstance(price, dict) or any(
            type(price.get(key)) not in (int, Decimal) for key in ("in_per_1k", "out_per_1k")
        ):
            raise ValueError(f"prices[{model!r}] must give numeric in_per_1k and out_per_1k")
        prices[model] = Price(
            in_per_1k=Fraction(price["in_per_1k"]), out_per_1k=Fraction(price["out_per_1k"]),
        )
    return prices


def new_state() -> State:
    return State(processed_lines=0, last_timestamp=None, exact_timestamps=True, timeouts={}, daily_usage={})


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
) -> dict | None:
    call = record.call
    if call["status"] == "error":
        return None
    model = call["model"]
    key = _state_key(model)
    if call["status"] == "ok":
        streak = state["timeouts"].pop(key, [])
        if len(streak) == threshold:
            return {
                "type": "recovered",
                "ts": call["ts"],
                "model": model,
                "request_ids": [call["request_id"]],
            }
        return None

    streak = state["timeouts"].setdefault(key, [])
    if len(streak) < threshold:
        streak.append(record.line_number)
        if len(streak) == threshold:
            return {
                "type": "timeout_streak",
                "ts": call["ts"],
                "model": model,
                "request_ids": [requests_by_line[line] for line in streak],
            }
    return None


def _budget_event(record: Record, state: State, config: Config) -> dict | None:
    call = record.call
    user = call["user"]
    day = record.timestamp.date().isoformat()
    days = state["daily_usage"].setdefault(_state_key(user), {})
    usage = days.setdefault(day, DailyUsage(tokens=0, alerted=False))
    usage["tokens"] += call["tokens_in"] + call["tokens_out"]
    budget = config["daily_token_budget"].get(user, config["default_daily_token_budget"])
    if usage["tokens"] > budget and not usage["alerted"]:
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


def process_records(records: list[Record], config: Config, state: State) -> list[dict]:
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
        timeout = _timeout_event(record, state, config["timeout_streak"], requests_by_line)
        if timeout is not None:
            events.append(timeout)
        budget = _budget_event(record, state, config)
        if budget is not None:
            events.append(budget)
    state["last_timestamp"] = ordered[-1].call["ts"]
    return events


def upgrade_state(state: State, records: list[Record], config: Config) -> State:
    if state.get("exact_timestamps") is True:
        return state

    # Rebuild the old cursor's counts and timestamp without printing old alerts.
    processed_lines = state["processed_lines"]
    upgraded = new_state()
    process_records([record for record in records if record.line_number <= processed_lines],
                    config, upgraded)
    upgraded["processed_lines"] = processed_lines
    return upgraded
