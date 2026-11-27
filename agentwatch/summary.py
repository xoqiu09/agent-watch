"""Summarize calls by user and by model."""

from collections import defaultdict
from decimal import Decimal
from fractions import Fraction

from .alerts import Prices
from .logread import Call, Log


def _nearest_rank(sorted_values: list[int | Decimal], percent: int) -> int | Decimal | None:
    if not sorted_values:
        return None
    rank = (len(sorted_values) * percent + 99) // 100
    return sorted_values[rank - 1]


def call_metrics(calls: list[Call]) -> dict:
    successful_latencies = sorted(call["latency_ms"] for call in calls if call["status"] == "ok")
    return {
        "calls": len(calls),
        "success_rate": len(successful_latencies) / len(calls) if calls else None,
        "timeouts": sum(call["status"] == "timeout" for call in calls),
        "errors": sum(call["status"] == "error" for call in calls),
        "p50_ms": _nearest_rank(successful_latencies, 50),
        "p95_ms": _nearest_rank(successful_latencies, 95),
        "tokens_in": sum(call["tokens_in"] for call in calls),
        "tokens_out": sum(call["tokens_out"] for call in calls),
    }


def call_cost(calls: list[Call], prices: Prices) -> tuple[Fraction | None, set[str]]:
    total = Fraction(0)
    unpriced = set()
    has_price = False
    for call in calls:
        model = call["model"]
        price = prices.get(model)
        if price is None:
            unpriced.add(model)
            continue
        has_price = True
        total += (call["tokens_in"] * price["in_per_1k"]
                  + call["tokens_out"] * price["out_per_1k"]) / 1000
    return (total if has_price or not calls else None), unpriced


def display_cost(amount: Fraction | None) -> Decimal | str:
    if amount is None:
        return "未定价"
    units = int(abs(amount) * 10_000 + Fraction(1, 2))
    rounded = Decimal(-units if amount < 0 else units).as_tuple()
    return Decimal((rounded.sign, rounded.digits, -4))


def summarize(
    log: Log,
    user_aliases: dict[str, str] | None = None,
    model_aliases: dict[str, str] | None = None,
    *,
    prices: Prices | None = None,
) -> dict:
    by_user = defaultdict(list)
    by_model = defaultdict(list)
    for record in log.records:
        call = record.call
        by_user[call["user"]].append(call)
        by_model[call["model"]].append(call)

    users = {}
    for user, calls in sorted(by_user.items()):
        metrics = call_metrics(calls)
        if prices is not None:
            cost, unpriced = call_cost(calls, prices)
            metrics["cost"] = display_cost(cost)
            metrics["unpriced_models"] = sorted(
                model_aliases[model] if model_aliases is not None else model for model in unpriced
            )
        users[user_aliases[user] if user_aliases is not None else user] = metrics

    models = {}
    for model, calls in sorted(by_model.items()):
        metrics = call_metrics(calls)
        if prices is not None:
            cost, _ = call_cost(calls, prices)
            metrics["cost"] = display_cost(cost)
        models[model_aliases[model] if model_aliases is not None else model] = metrics

    return {
        "by_user": users,
        "by_model": models,
        "bad_lines": len(log.bad_lines),
    }
