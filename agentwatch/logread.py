"""Read JSON Lines while keeping valid records and physical line numbers."""

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
from pathlib import Path
from typing import TypedDict


class _LogValidationError(ValueError):
    """A complete JSON row rejected by newer log validation rules."""


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
    utc_seconds: Decimal
    raw: str
    line_number: int


@dataclass
class Log:
    records: list[Record]
    bad_lines: list[tuple[int, str]]
    line_count: int
    prefix_digest: str | None = None
    content_digest: str | None = None

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


_TIMESTAMP_PARTS = re.compile(
    r"(?P<clock>(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{8}|[0-9]{4}-?W[0-9]{2}(?:-?[1-7])?)"
    r".[0-9:]+)(?:[.,](?P<fraction>[0-9]+))?"
    r"(?:(?P<zone_gap>[^0-9:Z+-][^Z+-]*)?"
    r"(?:(?P<utc>Z)|(?P<offset>[+-][0-9:]+)(?:[.,](?P<offset_fraction>[0-9]+))?))?",
    re.DOTALL,
)


def parse_timestamp(source: str) -> tuple[datetime, Decimal]:
    parts = _TIMESTAMP_PARTS.fullmatch(source)
    # Keep the supported date/time separator. Python can ignore a NUL near the offset.
    if "\0" in source and (parts is None or "\0" in (parts["zone_gap"] or "")):
        raise _LogValidationError("invalid ts")

    fraction = offset_fraction = "0"
    offset_sign = 1
    if parts is not None:
        fraction = parts["fraction"] or "0"
        offset_fraction = parts["offset_fraction"] or "0"
        zone = parts["offset"] or ("+00:00" if parts["utc"] else "")
        offset_minutes = zone[1:].replace(":", "")[2:4]
        if offset_minutes and int(offset_minutes) > 59:
            raise _LogValidationError("invalid ts time zone offset minutes")
        offset_sign = -1 if zone.startswith("-") else 1
        # Let fromisoformat validate any gap before the zone as written.
        if parts["zone_gap"] is None:
            # Python 3.10 accepts six-digit fractions in both the clock and offset.
            source = parts["clock"]
            if parts["fraction"] is not None:
                source += "." + fraction[:6].ljust(6, "0")
            source += zone
            if parts["offset_fraction"] is not None:
                source += "." + offset_fraction[:6].ljust(6, "0")

    try:
        timestamp = datetime.fromisoformat(source)
    except ValueError as exc:
        raise ValueError("invalid ts") from exc
    offset = timestamp.utcoffset()
    if offset is None:
        raise ValueError("ts must include a time zone")

    seconds = ((timestamp.toordinal() * 24 + timestamp.hour) * 60 + timestamp.minute) * 60
    seconds += timestamp.second - int(offset.total_seconds())
    # Subtract the full signed offset fraction without rounding or changing the local date.
    with localcontext() as context:
        context.prec = len(str(seconds)) + max(len(fraction), len(offset_fraction)) + 1
        utc_seconds = Decimal(seconds) + Decimal("0." + fraction)
        utc_seconds -= offset_sign * Decimal("0." + offset_fraction)
    return timestamp, utc_seconds


def _parse(raw: str, line_number: int) -> Record:
    try:
        # Object pairs stay in a tuple, distinct from JSON arrays.
        pairs = json.loads(raw, parse_float=Decimal, parse_constant=_reject_constant,
                           object_pairs_hook=tuple)
    except InvalidOperation as exc:
        raise ValueError("invalid JSON number") from exc
    if not isinstance(pairs, tuple):
        raise ValueError("expected a JSON object")
    data = {}
    for field, value in pairs:
        if field in data and field in Call.__annotations__:
            raise _LogValidationError(f"duplicate field: {field}")
        data[field] = value
    for field in Call.__annotations__:
        if field not in data:
            raise ValueError(f"missing field: {field}")

    for field in ("ts", "request_id", "user", "model"):
        if not isinstance(data[field], str) or not data[field].strip():
            raise ValueError(f"{field} must be a non-empty string")
    for field in ("request_id", "user", "model"):
        try:
            data[field].encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _LogValidationError(f"{field} must be valid UTF-8") from exc
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

    timestamp, utc_seconds = parse_timestamp(data["ts"])
    call = Call(
        ts=data["ts"],
        request_id=data["request_id"],
        user=data["user"],
        model=data["model"],
        latency_ms=latency,
        status=data["status"],
        tokens_in=data["tokens_in"],
        tokens_out=data["tokens_out"],
    )
    return Record(call, timestamp, utc_seconds, raw, line_number)


def read_log(path: Path, *, defer_partial_tail: bool = False, processed_lines: int = 0) -> Log:
    log = Log(records=[], bad_lines=[], line_count=0)
    digest = sha256() if defer_partial_tail else None
    if digest is not None and processed_lines == 0:
        log.prefix_digest = digest.hexdigest()
    with path.open("rb") as source:
        for line_number, line in enumerate(source, start=1):
            content = line.removesuffix(b"\n").removesuffix(b"\r")
            try:
                raw = content.decode("utf-8")
                log.records.append(_parse(raw, line_number))
            except (ValueError, RecursionError) as exc:
                # An older version could have already counted this complete row.
                saved_invalid_row = (line_number <= processed_lines
                                     and isinstance(exc, _LogValidationError))
                if defer_partial_tail and not line.endswith(b"\n") and not saved_invalid_row:
                    break
                log.bad_lines.append((line_number, str(exc)))
            log.line_count = line_number
            if digest is not None:
                # Adding a terminator to the last processed row preserves its content.
                digest.update(content)
                digest.update(b"\n")
                if line_number == processed_lines:
                    log.prefix_digest = digest.hexdigest()
    if digest is not None:
        log.content_digest = digest.hexdigest()
    return log
