"""Build a daily comparison and write the Chinese Markdown report."""

from datetime import date, timedelta
from decimal import MAX_EMAX, MIN_EMIN, Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from tempfile import NamedTemporaryFile

from .alerts import Config, Prices, new_state, process_records
from .logread import Record
from .summary import call_cost, call_metrics, display_cost


_MARKDOWN_ESCAPES = str.maketrans({character: "\\" + character for character in "\\`*_[]<>&~"})


def _day_metrics(records: list[Record], prices: Prices) -> tuple[dict, Fraction | None]:
    calls = [record.call for record in records]
    metrics = call_metrics(calls)
    cost, unpriced = call_cost(calls, prices)
    metrics["cost"] = display_cost(cost)
    metrics["unpriced_model_count"] = len(unpriced)
    return metrics, cost


def _public_metrics(metrics: dict) -> dict:
    fields = ("calls", "success_rate", "p95_ms", "tokens_in", "tokens_out",
              "cost", "unpriced_model_count")
    return {field: metrics[field] for field in fields}


def _success_rate(metrics: dict) -> Fraction:
    calls = metrics["calls"]
    return Fraction(calls - metrics["timeouts"] - metrics["errors"], calls)


def _p95_slower(current: int | Decimal, previous: int | Decimal) -> bool:
    previous = Decimal(previous)
    with localcontext() as context:
        context.prec = len(previous.as_tuple().digits) + 2
        context.Emin = MIN_EMIN
        context.Emax = MAX_EMAX
        return previous > 0 and current > previous * Decimal("1.3")


def daily_comparison(
    records: list[Record], day: date, prices: Prices,
) -> tuple[dict, list[Record]]:
    previous_day = day - timedelta(days=1) if day > date.min else None
    todays_records = []
    previous_records = []
    for record in records:
        record_day = record.timestamp.date()
        if record_day == day:
            todays_records.append(record)
        elif record_day == previous_day:
            previous_records.append(record)

    today, today_cost = _day_metrics(todays_records, prices)
    flags = []
    previous_metrics = None
    message = None
    if not previous_records:
        message = "没有前一天的数据"
    else:
        previous, previous_cost = _day_metrics(previous_records, prices)
        previous_metrics = _public_metrics(previous)
        if not todays_records:
            message = "当天没有数据"
        else:
            p95, previous_p95 = today["p95_ms"], previous["p95_ms"]
            if p95 is not None and previous_p95 is not None and _p95_slower(p95, previous_p95):
                flags.append("变慢")
            if _success_rate(previous) - _success_rate(today) > Fraction(1, 20):
                flags.append("失败变多")
            if today_cost is not None and previous_cost is not None and previous_cost > 0:
                if today_cost - previous_cost > previous_cost * Fraction(1, 2):
                    flags.append("费用上涨")

    report = {
        "date": day.isoformat(),
        "metrics": _public_metrics(today),
        "comparison": {
            "previous_date": previous_day.isoformat() if previous_day is not None else None,
            "previous": previous_metrics,
            "flags": flags,
            "message": message,
        },
    }
    return report, todays_records


def _row(label: str, metrics: dict) -> str:
    rate = metrics["success_rate"]
    rate_text = f"{rate:.2%}" if rate is not None else "—"
    p95 = metrics["p95_ms"]
    p95_text = f"{p95} ms" if p95 is not None else "—"
    cost = metrics["cost"]
    cost_text = cost if isinstance(cost, str) else f"{cost:.4f} 元"
    if metrics["unpriced_model_count"] and not isinstance(cost, str):
        cost_text += f"（另有 {metrics['unpriced_model_count']} 个模型未定价）"
    return (f"| {label} | {metrics['calls']} | {rate_text} | {p95_text} | "
            f"{metrics['tokens_in']} | {metrics['tokens_out']} | {cost_text} |")


def _inline_text(value: str) -> str:
    return " ".join(value.splitlines()).translate(_MARKDOWN_ESCAPES)


def _alert_line(event: dict) -> str:
    if event["type"] == "budget_exceeded":
        detail = (f"{_inline_text(event['user'])} 用量超额"
                  f"（{event['tokens']} / {event['budget']} token）")
    elif event["type"] == "timeout_streak":
        detail = f"{_inline_text(event['model'])} 连续超时"
    else:
        detail = f"{_inline_text(event['model'])} 恢复"
    requests = ", ".join(_inline_text(request_id) for request_id in event["request_ids"])
    return f"- {_inline_text(event['ts'])} {detail}，请求 {requests}"


def write_report(
    records: list[Record], day: date, config: Config, output: Path, *, bad_line_count: int = 0,
) -> None:
    report, todays_records = daily_comparison(records, day, config["prices"] or {})
    alerts = process_records(todays_records, config, new_state())
    comparison = report["comparison"]
    lines = [f"# 调用日报 {report['date']}", ""]
    if bad_line_count:
        lines.extend([f"本次读取跳过 {bad_line_count} 行，指标仅按有效记录计算", ""])
    lines.extend([
        "| 日期 | 调用数 | 成功率 | p95 | 输入 token | 输出 token | 费用 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        _row(report["date"], report["metrics"]),
    ])
    if comparison["previous"] is not None:
        lines.append(_row(comparison["previous_date"], comparison["previous"]))
    lines.extend(["", "## 与前一天比较", "",
                  comparison["message"] or "、".join(comparison["flags"]) or "无指标超过门槛",
                  "", "## 当天告警", ""])
    lines.extend(_alert_line(event) for event in alerts)
    if not alerts:
        lines.append("仅按当天调用重算，未触发告警")
    target = NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                dir=output.parent, delete=False)
    temporary_path = Path(target.name)
    try:
        with target:
            target.write("\n".join(lines) + "\n")
        temporary_path.replace(output)
    finally:
        temporary_path.unlink(missing_ok=True)
