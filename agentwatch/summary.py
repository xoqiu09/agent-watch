"""Summarize calls by user and by model."""

from collections import defaultdict

from .logread import Call, Log


def _nearest_rank(sorted_values: list[int | float], percent: int) -> int | float | None:
    if not sorted_values:
        return None
    rank = (len(sorted_values) * percent + 99) // 100
    return sorted_values[rank - 1]


def _metrics(calls: list[Call]) -> dict:
    successful_latencies = sorted(call["latency_ms"] for call in calls if call["status"] == "ok")
    return {
        "calls": len(calls),
        "success_rate": len(successful_latencies) / len(calls),
        "timeouts": sum(call["status"] == "timeout" for call in calls),
        "errors": sum(call["status"] == "error" for call in calls),
        "p50_ms": _nearest_rank(successful_latencies, 50),
        "p95_ms": _nearest_rank(successful_latencies, 95),
        "tokens_in": sum(call["tokens_in"] for call in calls),
        "tokens_out": sum(call["tokens_out"] for call in calls),
    }


def summarize(
    log: Log,
    user_aliases: dict[str, str] | None = None,
    model_aliases: dict[str, str] | None = None,
) -> dict:
    by_user = defaultdict(list)
    by_model = defaultdict(list)
    for record in log.records:
        call = record.call
        user = call["user"]
        model = call["model"]
        by_user[user_aliases[user] if user_aliases is not None else user].append(call)
        by_model[model_aliases[model] if model_aliases is not None else model].append(call)

    return {
        "by_user": {user: _metrics(calls) for user, calls in sorted(by_user.items())},
        "by_model": {model: _metrics(calls) for model, calls in sorted(by_model.items())},
        "bad_lines": len(log.bad_lines),
    }
