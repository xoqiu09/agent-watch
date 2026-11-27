# agent-watch 设计卡（M1）

## 要解决的问题
AI 应用上线后，要知道：谁在用、用了多少、慢不慢、哪里在失败；连续出问题时及时提醒，恢复了也要记下；任何一条提醒都能追回原始调用记录。这个项目用一份虚构调用日志演示这些。

全部数据虚构，不调用真实模型。Python 3.10+，只用标准库。

## 输入
JSON Lines 调用日志，每行一条：
`{"ts":"2026-11-27T10:00:00+08:00","request_id":"r0001","user":"alice","model":"model-a","latency_ms":820,"status":"ok|timeout|error","tokens_in":120,"tokens_out":340}`
配置 `config.json`：`{"timeout_streak":2,"daily_token_budget":{"alice":5000,"bob":3000},"default_daily_token_budget":4000}`。

## 目录
```
agentwatch/
  __main__.py  # 命令行
  logread.py   # 读日志；坏行记下行号和原因，不中断
  summary.py   # 汇总
  alerts.py    # 告警规则和状态
  sample.py    # 用固定种子生成演示日志
tests/
```

## 命令和行为
- `summary --log FILE`：按用户、按模型：调用数、成功率、超时数、错误数、延迟 p50/p95（只算成功调用，用最近秩法并在 README 写明）、输入输出 token 合计。坏行数单独列出。
- `alerts --log FILE --config CONFIG --state STATE`：按时间顺序处理新记录，输出告警事件（JSON Lines）：
  - 连续超时：同一模型连续 `timeout_streak` 次超时，发一条 `timeout_streak`（带这几次的 request_id）；这一段连续超时内只发一次；该模型下一次成功时发一条 `recovered`（带恢复的 request_id）。成功会把计数清零；`error` 不算超时，也不清零。
  - 用量超额：某用户当天（按日志里的时区日期）累计 token 第一次超过预算时发一条 `budget_exceeded`，当天不再重复；第二天重新计。
  - 状态文件记住已处理到第几行和各计数；对同一份日志再跑一次不重复发告警；日志追加新行后再跑只处理新行。
- `trace --log FILE REQUEST_ID`：打印这条原始记录；找不到时说找不到，退出码非 0。
- `sample --seed 7 --out FILE`：生成约 200 条演示日志，含至少一段连续超时和恢复、一次超额、两行坏数据。
- `export --log FILE --config CONFIG --out PATH`：汇总加告警事件加每条告警对应的原始记录，写成一个 JSON 供网站演示页使用；不含绝对路径、主机名、用户名；重复运行逐字节一致。

## 必须写的验收测试
1. 同一模型五次请求依次为 超时、成功、超时、超时、成功（timeout_streak=2）：前三次无告警；第 4 次恰好一条 `timeout_streak`，带第 3、4 次的 request_id；第 5 次恰好一条 `recovered`。
2. 两个模型交错超时，计数各算各的。
3. 预算边界：恰好等于预算不告警，超过 1 才告警；跨零点后重新计。
4. 坏行（非 JSON、缺字段、status 不认识）报告行号，其余照常处理。
5. 同一日志跑两次 `alerts` 第二次输出为空；追加三行后只处理这三行。
6. p50/p95 在给定小样本上等于手算值。
7. `trace` 能用告警里的 request_id 查回原始行。
8. `export` 无隐私信息、可重复。

证明测试不是摆设：把“成功清零”去掉、把“当天只发一次”去掉，各自跑测试，贴出失败，再改回。

## 不做
不接真实监控系统，不做网页（网站演示页另做），不发真实通知。

## M2

- 费用。配置新增 `prices`，按模型填写 `{"in_per_1k": 数字, "out_per_1k": 数字}`，单位为虚构的元。`summary` 增加按用户和按模型的费用，只在最终输出时四舍五入到 4 位小数。模型缺少价格时标记为“未定价”，其费用不计入合计，也不视作免费。
- 日报。`report --log FILE --config CONFIG --date YYYY-MM-DD --out FILE.md` 输出中文 Markdown，包含当天调用数、成功率、p95、token、费用及告警。按 `alerts` 的同一套规则从头计算当天告警，不读写状态文件。与前一天相比，p95 上升超过 30% 标记“变慢”，成功率下降超过 5 个百分点标记“失败变多”，费用上升超过 50% 标记“费用上涨”。前一天没有数据时写“没有前一天的数据”，跳过比较。
- 演示。`sample` 增加第二天的数据以演示比较。`export` 增加费用和日报的环比结果，供网站演示使用。
- 验收。核对手算费用、未定价处理、三个比较门槛的边界（恰好上涨 30% 时不标记“变慢”）、没有前一天数据、生成日报不改告警状态文件，以及导出包含费用和环比。保留 M1 的行为和测试，README 补充命令、每项新增测试的作用和本次未做的事项。运行 `/opt/homebrew/bin/python3 -m unittest -v` 及 README 的所有命令，并贴出输出。逐条故意改坏新规则、贴出测试失败，再恢复规则。
