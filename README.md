# agent-watch

当 AI 助手让客户久等或用量超出预算时，客服团队需要尽快发现并定位问题。

- 按用户和模型汇总用量、成功率、响应时间和估算费用。
- 记录连续超时、恢复和用量超额，凭请求编号查回原始记录。
- 生成日报，对照前一天，标出响应变慢、成功率下降或估算费用上涨。

## 怎么验证的

本地运行 `python3 -m unittest`，**59 项全部通过**。测试确认连续超时达到门槛才告警，告警后首次成功会记录恢复，每位用户每天首次超过 token 预算时提醒一次。沿用同一状态文件重跑同一份日志，不会重复告警。测试也核对了请求追溯、费用计算和日报比较。

在临时副本里故意让成功后超时计数不清零、超额反复告警、日报刚到门槛就标记变慢。对应测试都失败，恢复规则后通过。

## 快速体验

[在线演示](https://xoqiu.cn/watch-demo/index.html)

克隆后，在 `agent-watch` 的上一级目录运行以下命令。样例里故意放了坏行，程序会提示并继续输出汇总。

```sh
cd agent-watch
python3 -m agentwatch summary --log data/sample.jsonl --config config.json
```

演示使用虚构数据，模型和用户均为模拟对象。Owner 制定需求和验收标准，并指导 AI 编程工具实现。

## English overview

agent-watch is a Python CLI for support teams investigating slow AI responses and token budget overruns.\
It summarizes usage, latency, and estimated costs and compares them in a daily report.\
It flags repeated timeouts, recoveries, and token budget overruns and traces alerts to the original calls.\
All 47 tests passed locally, and deliberate rule changes made the relevant tests fail.\
The demo uses fictional data, models, and users. The owner set requirements and acceptance criteria and directed AI coding tools to build it.

## 运行演示

在仓库根目录运行。需要 Python 3.10 或更新版本。完整演示中的 `alerts` 需要 Unix 文件锁（`fcntl`）。整个演示只使用标准库，可离线运行。

```sh
demo="$(mktemp -d)"
python3 -m agentwatch sample --seed 7 --out "$demo/calls.jsonl"
cmp data/sample.jsonl "$demo/calls.jsonl"
python3 -m agentwatch summary --log "$demo/calls.jsonl" --config config.json
python3 -m agentwatch alerts --log "$demo/calls.jsonl" --config config.json --state "$demo/state.json"
python3 -m agentwatch alerts --log "$demo/calls.jsonl" --config config.json --state "$demo/state.json"
python3 -m agentwatch trace --log "$demo/calls.jsonl" r0001
python3 -m agentwatch report --log "$demo/calls.jsonl" --config config.json --date 2026-11-28 --out "$demo/report.md"
cat "$demo/report.md"
python3 -m agentwatch export --log "$demo/calls.jsonl" --config config.json --out "$demo/demo.json"
python3 - "$demo/demo.json" <<'PY'
import json
import sys
from decimal import Decimal
from pathlib import Path

demo = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"), parse_float=Decimal)
for model, row in demo["summary"]["by_model"].items():
    print(model, row["cost"])
report = demo["report"]
if report is None:
    print("没有日报")
else:
    print("日报费用", report["metrics"]["cost"])
PY
```

- `sample` 用固定种子生成首日 200 条有效调用和两条坏行，次日再加 5 条调用来演示比较。随仓库提供的 `data/sample.jsonl` 也是用种子 7 生成的，`cmp` 在两份文件一致时无输出。
- `summary` 按用户和模型输出调用数、成功率、超时数、错误数、延迟 p50/p95，以及输入和输出 token 合计。成功率以 0 到 1 的比例表示。延迟只取成功调用，升序排列后按最近秩法取第 `ceil(n × p)` 项，其中 `p` 为 0.50 或 0.95，排名从 1 开始。没有成功调用时，延迟分位数为 `null`。坏行的行号和原因写到标准错误，坏行总数单列。
- `summary` 传入带 `prices` 的配置后，按用户和模型增加 `cost`。每次调用的费用为 `(输入 token × in_per_1k + 输出 token × out_per_1k) / 1000`，单位为虚构的元，汇总完再四舍五入到 4 位小数。没有价格的模型标为“未定价”并排除在费用合计之外。用户的 `unpriced_models` 列出缺价模型。用户只有缺价调用时，`cost` 也显示“未定价”。省略 `--config` 或使用没有 `prices` 的旧配置时，保留原汇总格式。
- `alerts` 每次按时间处理新行，同一时间按文件顺序。连续超时按模型独立计算，达到门槛时发一次，成功会清零。已经告警的模型在下一次成功时发一条恢复事件，`error` 不增加也不清零超时数。每天的 token 按每条日志的时区日期和用户累计，严格超过预算才发告警，同一天只发一次。没有单独设置预算的用户使用默认值。
- `alerts` 每行输出一个 JSON 事件。对同一份只追加的日志和配置复用状态文件，第二次运行无输出。并发运行时，通过状态文件旁保留的 `.lock` 文件依次处理和保存进度，需要 Unix 文件锁。重跑会核对请求 ID，只把新行计入告警，并延续上一次的计数。追加记录的时间不能早于上次处理到的时间，否则以状态码 1 退出并保留原进度。末尾解析失败且没有换行的片段留到下次再读，带换行的坏行照常报告。状态文件用内部标识累计用户用量，用行号保留待关联的请求。
- `trace` 用告警里的 `request_ids` 逐条查原始日志，保留原来的字段和值及行内空白。找不到时输出原因并以状态码 1 退出。每条调用必须使用唯一的 `request_id`。发现重复时，`summary`、`alerts`、`trace`、`report` 和 `export` 报告冲突 ID 和行号，以状态码 1 退出。
- `report` 把指定日期的调用数、成功率、p95、输入和输出 token、费用及告警写成中文 Markdown。按日志时间戳的本地日期取当天记录，用同一套告警规则从头计算。若一串连续超时跨过午夜，日报只计算当天的超时。告警状态文件不会被读取或修改。报告写完后替换输出路径已有的文件或链接，链接指向的文件保持原样。每条告警占一行，标识中的换行显示为空格。与紧邻的前一天相比，p95 上升超过 30% 显示“变慢”，成功率下降超过 5 个百分点显示“失败变多”。前一天已定价费用大于零且涨幅超过 50% 时，显示“费用上涨”。比较使用舍入前的值。前一天没有记录时显示“没有前一天的数据”。
- `export` 将汇总和告警写入 `$demo/demo.json`，每条告警的 `records` 包含对应调用的标准字段。汇总使用配置中的价格，`report` 字段包含最新有记录日期的指标及环比，空日志的该字段为 `null`。用户名使用一致的别名，模型名和请求 ID 只保留演示格式（`model-a`、`model-b` 和 `r` 加 1 至 4 位数字），其他值也使用别名。生成别名时会跳过与私密原标识相同的值。导出会去掉绝对路径、主机名和额外日志字段。同样的输入重复导出，文件逐字节一致。输出文件须与日志、配置分别存放，指向同一文件的路径或链接会被拒绝。

## 验收测试

```sh
python3 -m unittest -v
```

| 测试 | 证明什么 |
| --- | --- |
| `test_01` | 五次指定调用中，前三次无告警，第四次给出第 3、4 次请求 ID，第五次给出恢复请求 ID。 |
| `test_02` | 两个模型交错超时时分别计数。时间乱序会先排序，错误不清零，持续超时只告警一次。 |
| `test_03` | 达到预算刚好不告警，超 1 才提醒，继续累加当天也只提醒一次。按日志时区跨零点重算，未配置的用户使用默认预算。 |
| `test_04` | 非 JSON、缺字段和未知状态各自报告实际行号和原因，有效行继续处理。 |
| `test_05` | 原样重跑无输出，追加三行只处理新行，超时和预算状态延续，坏行也计入读取位置。 |
| `test_06` | 20 个成功样本的 p50 为 100 ms、p95 为 190 ms，失败调用不影响延迟分位数。 |
| `test_07` | 用告警中的请求 ID 查回完整原始行，包括空白。不存在的 ID 以状态码 1 退出。 |
| `test_08`–`test_09` | 核对完整分组汇总和告警对应记录。私密的用户名、模型名、请求 ID 和附加字段不会漏入导出，重复导出逐字节一致。 |
| `test_10`–`test_14` | 状态文件不保留原号码，半行日志补全后仍会处理，较早的追加记录和重复请求 ID 会报错，导出不会覆盖日志或配置。 |
| `test_15`–`test_19` | 核对门槛为 3、模型独立清零、其他时区跨零点、追加后首次超额、三个样本的分位数，以及旧状态的延续。 |
| `test_cost_is_grouped_and_rounded_only_after_adding_calls` | 用手算核对输入和输出价格、用户与模型归集，以及加总后才舍入。 |
| `test_cost_rounds_exact_half_up` | 核对 `0.00005` 元四舍五入为 `0.0001` 元。 |
| `test_cost_retains_fourth_decimal_place_in_all_outputs` | 核对汇总、日报和导出都保留 `1099511627776.0001` 元的第四位小数。 |
| `test_unpriced_calls_are_marked_and_excluded_from_totals` | 核对缺价模型、部分缺价用户、全缺价用户和日报，区分缺价和实际费用为零。 |
| `test_p95_requires_more_than_thirty_percent_growth` | 核对 p95 上涨低于、等于和略高于 30% 时的标记。 |
| `test_p95_compares_exact_json_decimals_at_thirty_percent` | 核对日志中的长小数恰好上涨 30% 时跳过“变慢”标记。 |
| `test_success_requires_a_drop_of_more_than_five_points` | 核对成功率下降等于 5 个百分点时不标记，超过才标记。 |
| `test_success_compares_points_from_a_partial_success_rate` | 核对成功率从 50% 降到 46% 时未达到标记门槛。 |
| `test_cost_requires_more_than_fifty_percent_growth_before_rounding` | 核对费用上涨低于、等于和超过 50% 时的标记，即使两天显示的四位小数费用相同。 |
| `test_missing_previous_day_does_not_compare_with_an_older_day` | 核对缺少前一天时显示提示，跳过更早的记录。 |
| `test_report_recomputes_only_its_days_alerts_without_touching_state` | 先运行告警，再跑日报核对当天三种事件，并确认状态文件内容、修改时间和文件标识无变化。 |
| `test_report_does_not_read_alert_state` | 在日报运行时拦截对告警状态文件和状态读取函数的访问。 |
| `test_report_replaces_output_links_without_overwriting_state` | 核对日报输出路径上的硬链接和符号链接会被替换，并保留原状态文件的内容。 |
| `test_report_keeps_identifiers_on_their_alert_line` | 核对含换行和 Markdown 符号的标识仍显示在各自的告警行。 |
| `test_export_includes_aliased_costs_and_the_reports_comparison` | 核对用户和模型费用、缺价模型别名和环比结果，确认导出没有原始私密标识。 |
| `test_export_skips_aliases_that_match_private_identifiers` | 核对私密的 `model-1` 和 `request-1` 不会作为缺价清单或告警里的别名。 |
| `test_sample_produces_two_days_for_comparison` | 核对首日调用数、次日完整指标行和三条变化标记。 |

## 未包含

真实模型定价与账单、真实监控系统接入、真实模型调用、网页和真实通知都在本次范围之外。
