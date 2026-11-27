"""Read JSON Lines while keeping valid records and physical line numbers."""

import json
import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TypedDict


class Call(TypedDict):
    ts: str
    request_id: str
    user: str
    model: str
    latency_ms: int | Decimal
    status: str
    tokens_in: int
    tokens_out: int


@dataclass(frozen=True)
class Record:
    call: Call
    timestamp: datetime
    raw: str
    line_number: int


@dataclass
class Log:
    records: list[Record]
    bad_lines: list[tuple[int, str]]
    line_count: int

    def index_requests(self) -> dict[str, Record]:
        by_id: dict[str, Record] = {}
        for record in self.records:
            request_id = record.call["request_id"]
            previous = by_id.get(request_id)
            if previous is not None:
                raise ValueError(
                    f"duplicate request_id {request_id!r} on lines "
                    f"{previous.line_number} and {record.line_number}"
                )
            by_id[request_id] = record
        return by_id


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _parse(raw: str, line_number: int) -> Record:
    try:
        data = json.loads(raw, parse_float=Decimal, parse_constant=_reject_constant)
    except InvalidOperation as exc:
        raise ValueError("invalid JSON number") from exc
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    for field in Call.__annotations__:
        if field not in data:
            raise ValueError(f"missing field: {field}")

    for field in ("ts", "request_id", "user", "model"):
        if not isinstance(data[field], str) or not data[field].strip():
            raise ValueError(f"{field} must be a non-empty string")
    if data["status"] not in ("ok", "timeout", "error"):
        raise ValueError("unknown status")
    for field in ("tokens_in", "tokens_out"):
        if type(data[field]) is not int or data[field] < 0:
            raise ValueError(f"{field} must be a non-negative integer")

    latency = data["latency_ms"]
    if type(latency) not in (int, Decimal) or latency < 0:
        raise ValueError("latency_ms must be a non-negative number")
    if isinstance(latency, Decimal) and not math.isfinite(latency):
        raise ValueError("latency_ms must be finite")

    ts = data["ts"]
    try:
        timestamp = datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith("Z") else ts)
    except ValueError as exc:
        raise ValueError("invalid ts") from exc
    if timestamp.utcoffset() is None:
        raise ValueError("ts must include a time zone")

    call = Call(
        ts=ts,
        request_id=data["request_id"],
        user=data["user"],
        model=data["model"],
        latency_ms=latency,
        status=data["status"],
        tokens_in=data["tokens_in"],
        tokens_out=data["tokens_out"],
    )
    return Record(call, timestamp, raw, line_number)


def read_log(path: Path, *, defer_partial_tail: bool = False) -> Log:
    log = Log(records=[], bad_lines=[], line_count=0)
    with path.open("rb") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                raw = line.decode("utf-8").removesuffix("\n").removesuffix("\r")
                log.records.append(_parse(raw, line_number))
            except (ValueError, RecursionError) as exc:
                if defer_partial_tail and not line.endswith(b"\n"):
                    break
                log.bad_lines.append((line_number, str(exc)))
            log.line_count = line_number
    return log
