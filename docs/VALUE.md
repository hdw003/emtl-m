# 价值选股 + 中长线策略（0.5.0）

工作流分成四层：**候选范围 → 按时点筛选价值股池 → 长线策略择时/配置 → 风控与模拟执行**。
默认示例包含16只主板候选股票，支持配置1—200只；它不是全A实时扫描，也不是推荐名单。
候选范围是实验配置的一部分，修改范围或因子参数需要新建模拟账本。
价值股池的成员则会按月或季度自动变化，不需要修改候选配置。

## 默认筛选与评分

示例配置：[value_trend.toml](../examples/quant/value_trend.toml)。

| 条件 | 默认值 |
| --- | --- |
| 盈利与净资产 | 最新已知年报 EPS、每股净资产均 > 0 |
| PE / PB | <= 25 / <= 3，使用当时收盘价与最新可用年报每股指标 |
| 年度加权 ROE | >= 8% |
| 资产负债率 | <= 70% |
| 每股经营现金流 | > 0 |
| 上市时间 | 至少365个自然日 |
| 年报新鲜度 | 报告期末距决策日不超过550天 |
| 流动性 | 最近20个输入交易日的 close × 股数均值 >= 2000万元 |
| 状态 | 排除已知 ST / 退市状态；不对无行情、停牌日新开仓 |
| 行业 | 默认排除银行/保险/证券；每行业最多3只 |
| 股池大小 | 最多8只，不足时保留空余仓位 |

这是**年报口径的 PE/PB/ROE**，不是滚动 TTM，也不会把半年 EPS 当成年 EPS。
金融行业资产负债表和现金流的含义不同，默认不套用同一质量模型。
流动性使用 close×volume 估算，不冒充实际逐笔成交金额。

通过过滤的股票再按因子横截面分位数打分：盈利收益率方向40%、账面收益率方向30%、ROE方向30%。
PE/PB 越低、ROE 越高分数越高；并列因子使用平均秩，最终并列按代码稳定排序。
不按未来收益拟合权重，也不保证低估值一定带来超额收益。

默认每月第一个可观测交易日收盘重排；`frequency="quarterly"` 改为每季。
第一次有可用财务数据时可在月中初始化。若筛选为空，保持现金，等待下一次定期筛选。
新确认的 ST、非活跃状态或财务过期会提前移出，不必等待下次定期筛选。
自动状态来自公开公司资料的名称标识，不能覆盖所有交易所风险状态；严谨研究应导入完整状态历史。

## 与原有七种策略结合

在普通量化配置中增加 `[value]` 即启用动态股池。引擎只将当时股池内且当日可交易的股票传给策略。
示例采用价值股池 + 50/200 双均线、每周调仓。也可改成动量轮动、通道突破等已有策略。
调池导致成员变化时，会额外评估一次目标持仓；移出股票的目标仓位归零，下一开盘尝试卖出。
停牌、跌停、可用现金和实际剩余敞口仍约束执行，不把尚未卖出的股票当作已释放资金。

这既不是“便宜就立即买”，也不是绕过原有风控。最高单股仓位15%、总仓位90%、止损及回撤熔断等继续生效。
等权参照也使用同一个价值股池和风险限制，不拿未来筛选出的名单回填历史。

## 财务数据的时点约束

每条记录包含报告期 `period_end`、公告日 `published_on` 和真正可用于研究的日期 `known_on`。
系统要求 `period_end < published_on < known_on`，至少留一个自然日的公告缓冲。
决策只查询 `known_on <= 当日` 的版本；优先最新报告期，再取该报告期当时最新版本。
晚来的旧年报更正不会覆盖已知的新年报。

公开自动采集使用东方财富年报指标与公司基本资料，域名 `datacenter.eastmoney.com`；
历史K线可使用 `push2his.eastmoney.com`，或显式设置 `data_provider="tencent"`，使用
`web.ifzq.gtimg.cn` 的未复权日线。示例采用后者，因为实际验证中东方财富个别股票K线持续503。
腾讯请求按年切分避免返回条数上限，严格只接收 raw day，不把 qfqday/hfqday 当成真实成交价。
不自动混合两个数据源。保留环境代理和CA，TLS校验开启，GET仅有限重试。
公司基本资料接口替代了实际验证中持续502的实时批量元数据接口，不依赖付费账号。

**今天抓到的历史年报，不代表今天之前就拿到了这个版本。** 自动采集把 known_on 设为本次观察日，
不会回填成旧公告日。原始返回和观察时间在 SQLite 中归档。公开数据仍需独立核实；
不存在自动重建多年无修订财务时点历史的承诺。刚开始采集时，历史回测会在首个 known_on 前保持现金。
报告中的 `value_status` 会显示首个已知日期和有观察记录的交易日数量；`coverage="none"`
表示不能从该区间推断价值策略表现，空仓收益零不代表完成了价值历史验证。

适合长期回测的财务 CSV 应来自有时点保证的数据源，并包含当时的证券状态、行业和每股口径。
拆股或股份数变化后，每股 EPS/BVPS 必须与价格股份口径一致；本模块不会臆造调整因子。
年报指标更新较慢，不能用来解释所有季报变化；中期报告、TTM、红利和行业中性模型可在独立模型中扩展。

## 使用方式

在仓库根目录恢复环境：

```sh
uv sync --locked --extra tests
```

完全离线的合成示例（仅验证功能，所有财务和价格都标为合成）：

```sh
.venv/bin/emt quant value-demo --directory .quant/value-demo
.venv/bin/emt quant backtest --config .quant/value-demo/strategy.toml --output .quant/value-demo/backtest.json
.venv/bin/emt quant paper --config .quant/value-demo/strategy.toml --asof 2025-12-31 --database .quant/value-demo/paper.sqlite3 --output .quant/value-demo/paper.json
```

公开数据的首次准备与预览：

```sh
.venv/bin/emt quant value-capture --config examples/quant/value_trend.toml
.venv/bin/emt quant fetch --config examples/quant/value_trend.toml --start 2024-01-01 --end 2026-10-27
.venv/bin/emt quant value-pool --config examples/quant/value_trend.toml --output .quant/value-pool.json
```

`value-capture` 同日重复采集默认跳过；`--force` 可重新检查，但同一日期的冲突版本会拒绝覆盖。
需要修正当天记录时应保留原件，明确新的可用日期后导入，或建立新的实验；不能假造历史公告。
财务采集要求全部配置品种成功才写入一个事务，失败不会替换成部分股池。
价格下载同样完整成功后才替换CSV。财务库和模拟账本是两个事务边界，整个流水线失败可重跑，
不会重发实盘委托，因为这些命令不调用实盘适配器。

`value-pool` 是独立预览，输出排名、分数、PE/PB、年报/可用日期、落选原因和价格日期。
收盘前可以使用最近完成的收盘价预览；预览不等于当天收盘后的实际调仓股池，不发送订单。
所有快照按内容哈希保存在财务库，新的预览不会覆盖旧快照。

导入自己的历史时点 CSV：

```csv
symbol,period_end,published_on,known_on,listed_on,name,industry,annual_eps,book_value_per_share,roe,operating_cash_flow_per_share,debt_ratio,is_st,is_active,source
600004.SH,2022-12-31,2023-04-01,2023-04-02,2003-04-28,研究示例公司,研究行业,1.2,8.5,0.12,2.1,0.45,false,true,example_only
```

```sh
.venv/bin/emt quant value-import --config examples/quant/value_trend.toml --input /path/to/pit-financials.csv
```

CSV 示例是合成数据，不表示所写证券代码的真实财务。ROE和负债率用小数，0.12表示12%。
每股数据单位为元。缺指标必须留空，不能用零伪装未知；缺失记录会明确落选。
重复相同内容幂等，`symbol/period_end/known_on` 相同而内容不同会回滚整个导入。

## 定时运行与恢复

收盘后执行：

```sh
bash scripts/run_value_daily.sh
```

脚本用进程锁包住整个流水线，依次：每日财务采集 → 追加候选股日线 → 模拟盘重放 → 保存股池及报告。
财务每日采集用于归档新版本和状态，价值股池仍按配置的月/季频率重排，价格策略按自己的周/月频率运行。
文件分别为 `.quant/value-fundamentals.sqlite3`、`value-daily.csv`、`value-paper.sqlite3` 和 `value-paper.json`。

cron 示例（机器需支持 CRON_TZ，代理/CA环境变量也必须传给任务）：

```cron
CRON_TZ=Asia/Shanghai
30 16 * * 1-5 cd /workspace/emtl-m && bash scripts/run_value_daily.sh >> .quant/value-daily.log 2>&1
```

首次先 `mkdir -p .quant`。本项目不自动修改系统cron；云任务结束不保证仍在线，
长期无人值守需要持续在线主机及外部调度器。每天关注退出码、数据新鲜度、股池是否为空和 `halted`。

SQLite 账本校验价格和**当时已知**财务记录。追加未来记录不会改变过去已记账结果；
在旧日期插入或修订财务会拒绝继续旧账本。修改候选范围、价值阈值、评分或长线策略参数要新建账本。
备份配置、价格、财务库和模拟账本；不要删除历史或改哈希强行恢复。
原来未启用[value]的ETF模拟账本保持兼容。

## 验证与边界

测试覆盖财务日期、未来数据隔离、更正版本、排名和行业上限、定时调池、ST提前退出、
模拟盘恢复及未来财务追加。合成示例可以产生真实的模拟订单路径，但不构成策略盈利验证。
自动来源当前证券名单具有幸存者偏差，缺完整历史退市样本时不能声称全市场历史选股有效。
本次不执行账户登录、私有查询或实盘下单；实盘依然默认关闭，账户对账还需单独实现与验证。

本次真实采集和功能验证结果见 [VALUE_VALIDATION.md](VALUE_VALIDATION.md)。
