# emtl-m

东方财富证券（`jywg.18.cn`）网页交易接口的**现代 Python** 客户端。

协议逆向自 [riiy/emtl](https://github.com/riiy/emtl)（MIT）。本仓库在**保持网络协议细节不变**的前提下，
按现代工程规范重写：类型注解、无模块级全局可变状态、依赖注入、异常分层、可选验证码求解器、可用 CLI。

## 特性差异 vs 原版 riiy/emtl

| 原版问题 | emtl-m 处理 |
|---|---|
| 模块级全局 `session` / `_em_validate_key`，仅支持单账号 | 每 `EMTClient` 实例独立 Session + validatekey，可多账号并行 |
| 裸 `raise`、异常被吞 | 异常层级 `EMTError → LoginFailedError / LoginRequiredError / ProtocolError` |
| `login()` 泄漏 validatekey 到日志 | 日志不再打印敏感值 |
| 验证码强依赖 ddddocr 顶层导入 | 可注入 `CaptchaSolver`，ddddocr 延迟导入，可手动 `manual_code` |
| 无超时 | 全请求 `timeout`（默认 20s） |
| CLI 空壳 | 可用的 `emt` 命令：行情/查询/买卖/撤单 |
| `logging.basicConfig(force=True)` 污染调用方 | 库不配置根日志 |
| py3.10 混搭、缺类型 | `>=3.12`，`StrEnum` / PEP 695 / `match` / frozen+slots dataclass |

## 日线量化（0.6.0）

0.6.0 支持沪深北全市场动态发现，取消候选数量、价值池数量及行业数量上限；全量分页、并发采集、断点缓存、
逐只落选原因和按观察日归档名单，接入定期调池及模拟盘。提供七策略 × 三种调仓频率对比和实际换股统计。
首次运行与数据覆盖限制见[全市场指南](docs/FULL_MARKET.md)，实测结果见[验证记录](docs/FULL_MARKET_VALIDATION.md)。

0.5.0 新增[价值选股与定期动态股池](docs/VALUE.md)：按已知年报筛选低估值、盈利质量、
现金流与负债率，按月/季度更新股池，再结合原有七种长线策略。支持每日公开财务采集、
时点CSV导入、历史版本校验和带锁的价值模拟盘脚本。

新增 A 股与 ETF 的七类中长线策略：双均线、时间序列动量、动量轮动、通道突破、
布林均值回归、逆波动率配置及等权持有。支持含成本/滑点的下一开盘回测、仓位和回撤风控、
SQLite 可恢复模拟盘、公开日线下载及每日运行脚本。实盘适配器默认关闭。

```sh
emt quant demo --directory .quant/demo
emt quant backtest --config .quant/demo/strategy.toml --output .quant/demo/report.json
```

合成数据仅用于功能验证。配置、真实数据、公司行动、长期调度及手动实盘边界见
[量化运行指南](docs/QUANT.md)。

## 安装

```sh
pip install -e '.[captcha]'    # ddddocr（较重的离线 OCR）为可选依赖
pip install -e .               # 无 OCR：登录时传 manual_code 或自定义 solver
```

## 使用

```python
from emtl import EMTClient, Direction, QuoteMarket, TradeMarket

client = EMTClient()  # 或传入 username/password；缺省读 EM_USERNAME / EM_PASSWORD
client.login()        # 拉验证码图 → OCR → RSA 登录 → 缓存 validatekey

client.query_asset_and_position()            # 资产持仓（.json / .ok / .data）
client.query_orders()                        # 当日委托
client.query_trades()                        # 当日成交
client.query_funds_flow(100, "2026-08-01", "2026-09-01")
resp = client.create_order("600000", Direction.BUY, TradeMarket.SH, 10.21, 100)
resp.ok          # True 当后端 Status == 0
client.cancel_order("20240520_130662")       # 撤单

client.last_price("600000", QuoteMarket.SH)  # 行情，无需登录

# —— 官网实测新功能（2026-09，登录后走完整 validatekey）——
url = client.export_history_file("1")        # 导出记录三连 → 下载 URL
client.credit_stock_info("600519", "SH")     # 融资融券(信用)持仓查询
client.customer_fxqdj()                      # 风险/反洗钱测评状态
```

注意两套市场码：**下单**用 `TradeMarket`（`HA`/`SA`），**行情/融资融券**用 `QuoteMarket`/`SH`/`SZ`。

下单默认 `strict=True`：后端拒绝（`Status != 0`）抛 `BusinessError`（带 Message/Data），传 `strict=False` 可拿原始响应自判。登录失败会带验证码换图自动重试（`login(max_attempts=3)`，手动验证码一次一试）。

## 开发环境（推荐 uv + 3.12，全依赖有轮子、免本地编译）

```sh
uv python install 3.12
uv venv --python 3.12
uv pip install -p .venv/bin/python requests rsa pytest ruff
.venv/bin/python -m pytest -q
```

CLI：

```sh
emt price 600000
emt query asset                # 需登录
emt query orders
emt buy 600000 10.21 100
emt sell 600000 10.21 100 --market SH
emt cancel 20240520_130662
emt export 1                    # 导出记录 → 下载 URL
emt credit 600519               # 融资融券持仓
emt fxqdj                       # 风险/反洗钱测评状态
```

## 登录流程（逆向要点）

1. `GET /Login/YZM?randNum=<随机数>` 取验证码图 → OCR（或手动）
2. `POST /Login/Authentication?validatekey=`，密码 RSA（PKCS1v15 + 硬编码公钥）后 Base64，
   表单含 `randNumber`/`identifyCode`/`duration`/`type=Z`
3. `GET /Trade/Buy` 从 `id="em_validatekey"` 抠出 validatekey
4. 此后所有查询/下单 URL 拼接该 validatekey，表单走 `application/x-www-form-urlencoded`

## ⚠️ 风险声明

这是**非官方逆向接口**：东财可随时改端点/加密导致失效；高频调用有账号风控风险。
仅建议低频率、个人研究/模拟盘场景；真实下单后果自负。MIT License。

## 测试

```sh
pip install pytest
pytest        # 离线：加密形状、模型、登录/请求构建（假传输）
```
