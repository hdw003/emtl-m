# 日线量化开发和运行指南

从 0.4.0 起，`emtl.quant` 在原有交易客户端之上提供 A 股和 ETF 的中长线研究、
回测、可恢复模拟盘，以及手动启用的实盘限价单适配器。默认命令不会登录账户或发送交易单。
所有策略均为多头、无杠杆。核心只使用标准库和项目已有 requests，不引入 pandas、数据库服务或后台线程。

## 五分钟离线体验

在仓库根目录安装并运行：

```sh
uv sync --locked --extra tests
.venv/bin/emt quant strategies
.venv/bin/emt quant demo --directory .quant/demo
.venv/bin/emt quant backtest --config .quant/demo/strategy.toml --output .quant/demo/backtest.json
.venv/bin/emt quant paper --config .quant/demo/strategy.toml --asof 2025-12-31 --database .quant/demo/paper.sqlite3 --output .quant/demo/paper.json
```

重复执行最后一条命令，`new_sessions` 为 0，不会重复记账。`demo` 使用固定随机种子的合成数据，
包含周末过滤但不模拟完整交易所节假日；它只证明软件流程可运行，不证明策略盈利。
示例目录已有文件时 `demo` 会拒绝覆盖。

## 内置策略

| 名称 | 信号与退出 | 主要参数及典型研究尺度 |
| --- | --- | --- |
| `buy_hold` | 等权持有当前可用资产；依调仓频率恢复目标权重 | 基线配置，仍受组合风控约束 |
| `dual_ma` | 短期均线高于长期均线时持有，否则现金 | `fast=50, slow=200` |
| `time_series_momentum` | 各资产过去收益为正时等权持有 | `lookback=126`，可设 252 |
| `momentum_rotation` | 按历史收益排名选前 N 个，默认排除负收益 | `lookback=126, skip=21, top_n=2` |
| `donchian` | 收盘突破之前 N 日最高收盘价买入；跌破退出窗口最低收盘价退出 | `lookback=126, exit_window=20` |
| `bollinger` | 收盘低于均值若干标准差时买入，回到退出 z 分数时卖出 | `lookback=20, entry_z=2, exit_z=0` |
| `inverse_volatility` | 使用历史收益标准差的倒数分配权重 | `lookback=60`；不是协方差风险平价优化器 |

所有窗口以输入数据中的交易日计数。`skip=21` 会在动量计算时跳过最近 21 个观测值。
动量使用简单收益，`absolute_filter=true` 要求大于零。相同得分按代码排序，便于重现实验。
均值回归和通道策略使用实际持仓决定是否继续持有；执行失败不会被误认为已经建仓。
这里实现的是代表性价格策略，不包含需要财务时点数据库的价值、质量或红利选股因子。

切换策略可以编辑 `[strategy]`，或只在回测时覆盖名称：

```sh
.venv/bin/emt quant backtest --config examples/quant/etf_rotation.toml --strategy dual_ma --output .quant/trend.json
```

该覆盖保留其他参数。因此切到 `bollinger` 等策略时，建议建立单独配置并明确设置窗口。
修改参数后运行模拟盘必须使用新的数据库，不会覆盖原实验账本。

## 数据、复权与交易日

支持 CSV 导入，也提供公开的东方财富日线接口：

```sh
.venv/bin/emt quant fetch --config examples/quant/etf_rotation.toml --start 2020-01-01 --end 2026-10-27
.venv/bin/emt quant backtest --config examples/quant/etf_rotation.toml --output .quant/rotation-backtest.json
```

后续更新加 `--append`，只请求已有数据之后的日期。多个品种全部下载成功后才原子替换文件，
其中一个失败不会留下半份数据。目标文件已存在时，未加 `--append` 会报错。网络请求保留环境代理、
CA 和 TLS 校验，需要 HTTPS 访问 `push2his.eastmoney.com`。日线下载拒绝尚未收盘的日期；
以 Asia/Shanghai 的 16:00 为当日可用截止点，并不保证数据商届时已经完成最终修订。

CSV 必须包含：

```csv
date,symbol,open,high,low,close,volume,buy_allowed,sell_allowed,split_ratio,cash_dividend
2024-01-02,510300.SH,3.50,3.55,3.48,3.52,10000000,1,1,1,0
```

前七列必填，其余默认可交易、无送转、无派息。`symbol` 明确使用六位代码和 `.SH`/`.SZ`，
避免 ETF 的 5 开头被误判为深市。价格为人民币元，`volume` 为股/份；下载适配器会把接口的手转换为股。
不接受 NaN、无穷值、非法 OHLC、重复日期/品种或拼错的列。输入会按日期和代码排序。
`buy_allowed`/`sell_allowed` 用 0/1 或 true/false 表示额外的方向性交易限制。

使用**未复权价格**，`split_ratio` 表示除权日新股数/旧股数，`cash_dividend` 为每旧股派现金额。
引擎在除权日开盘前派现金、调整股数，并从当时起缩放已知历史收盘价用于后续信号。
不会在事件发生前修改过去的交易记录。碎股公司行动目前会明确报错，需另行建模。

公开历史接口**不提供公司行动**，因此下载 CSV 的这两列默认 1 和 0。
正式分析前要从可信数据源补齐分红、拆并股、送转及其准确日期。不能直接把事后前复权价格当作真实成交价格，
也不能将未经公司行动补齐的多年结果当作可靠收益统计。已经入账的历史若需修订，应保留旧账本并新建实验。

回测交易日取所输入资产日期的并集；资产上市前没有信号，缺失日沿用最后估值且无法成交。
默认资产池是静态配置，需要用户自行处理退市样本、幸存者偏差和历史成员变化。
`start` 前的数据用于预热，不交易；`end` 可用于固定样本区间。没有足够历史的资产保持现金。

## 回测执行和风控

- 收盘后计算信号，下一条全市场日线的开盘撮合。月调仓在每月首个观测日收盘生成信号；周调仓用 ISO 周。
- 先卖后买，费用不足时缩减买入量，不借钱。买入按配置整手，清仓可以卖出完整零股。
- 每品种每天至多一次成交，买入后最早下一交易日卖出，符合本日线流程的 T+1。
  `settlement_days=0` 的 ETF 在本引擎中也不做日内往返。
- 收取比例佣金、最低佣金、股票卖出印花税；ETF 默认无印花税。费率是可配置的固定假设，
  不自动恢复历史税率，也不默认包含各券商独立规费。
- 对买卖分别施加不利滑点并按最小价格单位取整；流动性上限使用**上一已知交易日**的成交股数，
  不用当前成交日尚未发生的全天成交量决定开盘仓位。
- 停牌、缺 K 线、显式限制，以及开盘处于方向性涨跌停时拒绝撮合。涨跌停采用每品种固定比例、
  按 tick 取整的保守模型；不模拟盘口队列、IPO 无涨跌幅期间、ST/板块规则的历史切换或撮合成交率。
  这些情况需要预处理数据或扩展执行模型。
- 未成交的目标不会永久挂单：下次计划调仓再评估；止损和回撤清仓会在后续日继续尝试。

组合限制包括 `max_position` 单资产上限、`max_gross` 总仓位上限、可选年化目标波动率、
成本基准止损、收盘高水位追踪止损、冷却期，以及最大组合回撤熔断。
目标波动率基于对齐日收益的组合标准差，只降低风险，不加杠杆。
权重封顶之后不把被削减的仓位重新分配，剩余持有现金。

止损和熔断均在收盘观察，下一开盘尝试执行，无法保证实际损失恰好等于阈值；跳空、停牌和跌停可令损失扩大。
回撤熔断触发后，当前实验永久禁止新开仓。重新启动投资实验需要显式决定新的开始日期和参数，并使用新账本。

JSON 报告包含每日权益、现金、回撤、每笔模拟成交及费用、拒绝原因、持仓和下一次目标权重。
`next_weights=null` 表示当日无调仓信号，`{}` 表示目标为空仓。
统计包含总收益、按日历时间年化收益、252 日年化波动率、零利率 Sharpe 和最大回撤。
非 buy_hold 的回测还输出同费用、同风控约束的等权持有参照；它不是市场指数基准。
短样本年化指标不稳定。建议固定训练/验证/测试时间段，保留参数实验记录，比较样本外表现和成本敏感性，
不要按完整历史收益选择参数后仍把同一历史称作样本外验证。

## 每日模拟盘和持续运行

单次收盘后运行（无账户凭据）：

```sh
.venv/bin/emt quant daily --config examples/quant/etf_rotation.toml --start 2020-01-01 --database .quant/etf-paper.sqlite3 --output .quant/etf-paper.json
```

也可只消费已准备好的 CSV：

```sh
.venv/bin/emt quant paper --config examples/quant/etf_rotation.toml --database .quant/etf-paper.sqlite3 --output .quant/etf-paper.json
```

`--asof YYYY-MM-DD` 可以重放到历史日期，但不允许晚于已完成的交易日。
默认取上海时间 16:00 以后的当日，否则前一日；周末和假日依数据中实际存在的日期处理。
所有数据或任何已持仓资产超过 10 个自然日未更新会报错，需排查数据源、停牌或超长休市，不能把静默失败当作运行成功。

模拟盘用相同引擎从固定起点确定性重放，适合数千个日线交易日；目前没有增量计算缓存。
SQLite 的事务和唯一键确保重启或并发重跑不重复入账。已处理历史通过累计哈希及权益/现金校验，
配置变化、历史被改写、日期倒退均会拒绝继续。账本保存成交、每日权益、事件、最后截止日期和待执行目标；
报告写入采用临时文件后原子替换。进程中断后直接重跑同一命令，不需人工修改账本。

Linux 持续运行脚本带 `flock`，应在收盘后由 cron/systemd 等外部调度器启动：

```sh
bash scripts/run_quant_daily.sh
```

cron 示例（服务器需支持 `CRON_TZ`，并保留本环境所需的代理和 CA 环境变量）：

```cron
CRON_TZ=Asia/Shanghai
20 16 * * 1-5 cd /workspace/emtl-m && bash scripts/run_quant_daily.sh >> .quant/daily.log 2>&1
```

首次配置日志重定向前先 `mkdir -p .quant`。脚本失败时返回非零状态；调度器应监控退出码、日志、
报告日期、连续失败以及 `halted=true`。本仓库不自动安装系统 cron，不假设云任务结束后进程仍存活；
长期无人值守需要你将脚本部署到持续在线的机器。保存云环境不能替代任务调度器。

定期备份配置、原始 CSV 和 SQLite 账本。并发运行时使用 SQLite backup API，而不是直接复制写入中的文件。
`uv.lock` 固定依赖版本，使用 `uv sync --locked --extra tests` 恢复开发环境，需要 OCR 时再加 `--extra captcha`。
依赖升级通过显式更新锁文件、测试和代码审查完成。`engine_model_version` 纳入配置指纹，
改变交易/风控语义时应提升模型版本并新建实验账本。
维护升级时先备份，运行完整测试与固定数据重放，在副本账本验证后切换；账本校验失败时保留原件分析，
不要删记录或修改校验值强行通过。修改策略、风险、费用或开始日期时采用新的配置文件和数据库。

## 手动接入实盘

`EMTBroker` 默认 `enabled=False`，回测、paper、daily 都不调用它，也不存在自动实盘 CLI。
`AccountSnapshot` 和 `plan_rebalance` 可以从明确对账后的现金、总股数、可卖股数、及时行情生成限价单计划。
计划校验报价/账户时间戳、T+1 可卖数量、整手及买入费用；不会提前花掉尚未成交卖单的预计款项。
应先处理卖单、核实成交和余额，再用新快照生成买单。

接线示意（不应直接当成无人值守脚本运行）：

```python
from pathlib import Path
from emtl import EMTClient
from emtl.quant import EMTBroker, OrderIntent

client = EMTClient()  # 账户凭据从安全环境配置读取，不写入配置文件
# client.login()     # 手动验证登录、持仓字段、可卖余额与成交回报后再启用
broker = EMTBroker(client, Path(".quant/account-order-journal.sqlite3"))
# 默认调用 submit 会拒绝发送。
# broker = EMTBroker(client, Path(".quant/account-order-journal.sqlite3"), enabled=True, max_order_value=10000)
# broker.submit(OrderIntent("510300.SH", "buy", 100, 3.5), request_id="accountA/2026-10-28/510300.SH/buy/001")
```

下单前先持久化稳定的 request_id。相同 ID 再次调用会拒绝；超时、进程崩溃或状态不明确的请求
不能通过换 ID 自动重发，必须核实券商委托和成交。适配器只记录 submitted/unknown，不将委托受理当作成交。
券商接口没有本项目可依赖的端到端幂等键，因此这不是“恰好一次”成交保证。
每个账户使用独立 journal。真实账户字段和成交回报的自动映射、委托撤单管理尚未实现或验证，
需要在你授权的账户环境中补充对账适配，而不能臆造响应字段。实盘前还需按品种验证交易时间和具体交易规则。

## 扩展和维护边界

模块职责：`models` 值对象，`data` 数据源，`strategies` 纯策略，`risk` 组合限制，
`engine` 事件循环，`planning` 订单规划，`paper` 账本，`execution` 手动券商桥接，`report` 报告。
实现 `Strategy.weights(history, held)` 即可使用自定义策略；只传入当时已知的历史。
实现 `DailyProvider.fetch(instrument, start, end)` 即可替换数据源。不要在策略里登录或下单。

```sh
.venv/bin/python -m pytest
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src/emtl/quant tests/quant
uv build
```

CI 在 Python 3.12、3.13 执行离线测试、lint 和打包，不需要券商账户或访问真实交易接口。
模型变更应增加手算账务测试、时间前缀不变性测试和账本兼容性测试；修改数据源要使用响应 fixture 验证字段和单位。
不内置收益承诺、选股推荐或自动优化器。未经完整数据治理、样本外评估和账户对账，不能将功能测试通过理解为可以放心实盘。
