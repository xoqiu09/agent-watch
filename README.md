# agent-watch

AI 应用上线后，可以用调用日志看谁在用、用量多大、响应多快，以及哪些模型在失败。agent-watch 用一份虚构日志演示如何汇总这些数据，怎样发现连续超时、恢复和用量超额，以及怎样从告警追回原始调用。

## 运行演示

在仓库根目录运行。需要 Python 3.10 或更新版本，只使用标准库，整个演示离线运行。

```sh
demo="$(mktemp -d)"
python3 -m agentwatch sample --seed 7 --out "$demo/calls.jsonl"
cmp data/sample.jsonl "$demo/calls.jsonl"
python3 -m agentwatch summary --log "$demo/calls.jsonl"
python3 -m agentwatch alerts --log "$demo/calls.jsonl" --config config.json --state "$demo/state.json"
python3 -m agentwatch alerts --log "$demo/calls.jsonl" --config config.json --state "$demo/state.json"
python3 -m agentwatch trace --log "$demo/calls.jsonl" r0001
python3 -m agentwatch export --log "$demo/calls.jsonl" --config config.json --out "$demo/demo.json"
```

- `sample` 用固定种子生成 200 条有效调用和两条坏行。随仓库提供的 `data/sample.jsonl` 也是用种子 7 生成的，`cmp` 在两份文件一致时无输出。
- `summary` 按用户和模型输出调用数、成功率、超时数、错误数、延迟 p50/p95，以及输入和输出 token 合计。成功率以 0 到 1 的比例表示。延迟只取成功调用，升序排列后按最近秩法取第 `ceil(n × p)` 项，其中 `p` 为 0.50 或 0.95，排名从 1 开始。没有成功调用时，延迟分位数为 `null`。坏行的行号和原因写到标准错误，坏行总数单列。
- `alerts` 每次按时间处理新行，同一时间按文件顺序。连续超时按模型独立计算，达到门槛时发一次，成功会清零。已经告警的模型在下一次成功时发一条恢复事件，`error` 不增加也不清零超时数。每天的 token 按每条日志的时区日期和用户累计，严格超过预算才发告警，同一天只发一次。没有单独设置预算的用户使用默认值。
- `alerts` 每行输出一个 JSON 事件。对同一份只追加的日志和配置复用状态文件，第二次运行无输出。重跑会核对请求 ID，只把新行计入告警，并延续上一次的计数。追加记录的时间不能早于上次处理到的时间，否则以状态码 1 退出并保留原进度。末尾解析失败且没有换行的片段留到下次再读，带换行的坏行照常报告。状态文件用内部标识累计用户用量，用行号保留待关联的请求。
- `trace` 用告警里的 `request_ids` 逐条查原始日志，保留原来的字段和值及行内空白。找不到时输出原因并以状态码 1 退出。每条调用必须使用唯一的 `request_id`。发现重复时，`alerts`、`trace` 和 `export` 报告冲突 ID 和行号，以状态码 1 退出。
- `export` 将汇总和告警写入 `$demo/demo.json`，每条告警的 `records` 包含对应调用的标准字段。用户名使用一致的别名，模型名和请求 ID 只保留演示格式（`model-a`、`model-b` 和 `r` 加 1 至 4 位数字），其他值也使用别名。导出会去掉绝对路径、主机名和额外日志字段。同样的输入重复导出，文件逐字节一致。输出文件须与日志、配置分别存放，指向同一文件的路径或链接会被拒绝。

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

## M1 未包含

真实监控系统接入、真实模型调用、网页和真实通知都在本次范围之外。

## English overview

agent-watch is an offline Python CLI for synthetic AI call logs. It summarizes usage and latency, reports timeout streaks, recoveries and daily token budget crossings, and traces alerts to their source records. It also exports repeatable demo JSON with aliases for private identifiers. Python 3.10+ and the standard library are sufficient.
