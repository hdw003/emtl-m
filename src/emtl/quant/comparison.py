"""Reproducible strategy comparisons with explicit present-pool hindsight bias."""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import asdict, replace
from pathlib import Path

from .config import QuantConfig
from .data import load_csv
from .report import report, write_json
from .strategies import STRATEGIES
from .value import ValueStore


def compare(
    config: QuantConfig, directory: Path, start: dt.date, end: dt.date, *, progress=None
) -> dict:
    if config.value is None:
        raise ValueError("comparison requires a value policy and dated financial observations")
    if start > end:
        raise ValueError("start must be <= end")
    directory.mkdir(parents=True, exist_ok=True)
    notify = progress or (lambda payload: None)
    bars = load_csv(config.data)
    facts = ValueStore(config.value.fundamentals).load()
    first = min((f.known_on for f in facts), default=None)
    if first is None or first > end:
        raise ValueError("no financial observations available by end date")
    actual, reference = [], []
    pool = None
    for name in STRATEGIES:
        spec = replace(config.strategy, name=name)
        if name == "momentum_rotation":
            spec = replace(spec, lookback=126, skip=21, top_n=3)
        elif name == "bollinger":
            spec = replace(spec, lookback=20)
        elif name == "inverse_volatility":
            spec = replace(spec, lookback=60)
        else:
            spec = replace(spec, lookback=126)
        realconfig = replace(config, strategy=spec, start=max(first, start), end=end)
        strict = realconfig.engine().run(bars, start=realconfig.start, end=end)
        payload = report(strict, realconfig)
        payload["study_kind"] = "point_in_time_observed_history"
        write_json(directory / f"actual-{name}.json", payload)
        actual.append(
            {
                "strategy": name,
                "metrics": payload["metrics"],
                "activity": payload["activity"],
                "value_status": strict.value_status,
                "target_stock_count": len(strict.next_weights or {}),
                "target_weights": strict.next_weights,
            }
        )
        current = set(strict.universe_history[-1]["selected"]) if strict.universe_history else set()
        if pool is not None and current != pool:
            raise ValueError("strategy comparison must use the same final value pool")
        pool = current
        frozen = [b for b in bars if b.symbol in current]
        ranking_snapshot = next(
            (snapshot for snapshot in reversed(strict.universe_history) if "ranking" in snapshot),
            None,
        )
        frozen_ranking = (
            {row["symbol"]: row["score"] for row in ranking_snapshot["ranking"]}
            if ranking_snapshot
            else {}
        )
        if name == STRATEGIES[0]:
            write_json(
                directory / "frozen-current-ranking.json",
                {
                    "ranking_observed_date": ranking_snapshot["date"] if ranking_snapshot else None,
                    "warning": "Frozen future-known value scores for a hindsight-biased diagnostic only.",
                    "scores": frozen_ranking,
                },
            )
        if not current:
            raise ValueError("no current value candidates available for a frozen-pool comparison")
        for frequency in ("monthly", "weekly", "daily"):
            historical = replace(
                config,
                value=None,
                market=None,
                instruments=tuple(i for i in config.instruments if i.symbol in current),
                strategy=spec,
                rebalance=frequency,
                start=start,
                end=end,
            )
            engine = historical.engine(
                selection_ranking=frozen_ranking if config.portfolio else None
            )
            engine.fractional_action = config.market.fractional_action if config.market else "error"
            result = engine.run(frozen, start=start, end=end)
            diagnostic = report(result, historical)
            diagnostic["study_kind"] = "biased_current_pool_historical_reference"
            diagnostic["warning"] = (
                "Current selected identities and value scores are fixed retrospectively. Financial and membership look-ahead and survivorship bias are present. This does not validate historical dynamic value returns."
            )
            diagnostic["assumptions"]["fractional_action"] = engine.fractional_action
            write_json(directory / f"biased-static-{frequency}-{name}.json", diagnostic)
            stats = diagnostic["activity"]
            reference.append(
                {
                    "strategy": name,
                    "frequency": frequency,
                    **diagnostic["metrics"],
                    **{
                        key: value
                        for key, value in stats.items()
                        if not isinstance(value, (dict, list))
                    },
                    "halted": result.halted,
                    "final_position_count": len(diagnostic["positions"]),
                }
            )
            notify(
                {
                    "stage": "comparison",
                    "strategy": name,
                    "frequency": frequency,
                    "return": diagnostic["metrics"]["total_return"],
                }
            )
    with (directory / "biased-static-reference.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=list(reference[0]))
        writer.writeheader()
        writer.writerows(reference)
    by_symbol = {f.symbol: f for f in facts}
    with (directory / "current-pool.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(["symbol", "name", "industry"])
        writer.writerows((s, by_symbol[s].name, by_symbol[s].industry) for s in sorted(pool))
    payload = {
        "start": str(start),
        "end": str(end),
        "first_financial_observation": str(first),
        "current_pool_count": len(pool),
        "point_in_time": actual,
        "portfolio_policy": asdict(config.portfolio) if config.portfolio else None,
        "biased_static_reference": reference,
        "warning": "Historical static reference uses the current pool and current value scores: look-ahead and survivorship bias. Forward observation history is required to evaluate dynamic value selection.",
    }
    write_json(directory / "comparison.json", payload)

    def percent(value):
        return "—" if value is None else f"{value * 100:.2f}%"

    lines = [
        f"# 全市场股池策略诊断：{start} 至 {end}",
        "",
        f"当前候选股池 {len(pool)} 只；财务首次留档 {first}。",
        f"实际组合预算：最多{config.portfolio.max_positions}只；候选股池无限制。"
        if config.portfolio
        else "未配置独立持仓选择层。",
        "",
        "**以下历史收益使用当前股池与当前价值评分回看，存在未来成员、未来因子和幸存者偏差，不能代表历史动态价值选股的真实收益。**",
        "",
        "包含佣金、最低佣金、卖出印花税、滑点及公开分红送转；不含个人分红税，现金于除息日入账。风险退出可发生在周/月调仓之外。",
        "",
        "| 策略 | 计划调仓 | 累计收益 | 年化 | 最大回撤 | 成交天数 | 换股天数 | 平均已结束持仓（交易日） |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in reference:
        holding = row["mean_completed_holding_sessions"]
        lines.append(
            f"| {row['strategy']} | {row['frequency']} | {percent(row['total_return'])} | {percent(row['annualized_return'])} | {percent(row['max_drawdown'])} | {row['actual_trading_days']} | {row['replacement_days']} | {holding:.1f} |"
            if holding is not None
            else f"| {row['strategy']} | {row['frequency']} | {percent(row['total_return'])} | {percent(row['annualized_return'])} | {percent(row['max_drawdown'])} | {row['actual_trading_days']} | {row['replacement_days']} | — |"
        )
    lines += [
        "",
        "成交天数指有实际成交的日期；换股天数指同日既有从零建仓又有完整清仓，首次建仓不计换股；持仓均值排除尚未卖完的持仓。",
        "",
        "严格时点结果见 actual-*.json；逐笔成交、持仓退出区间及每日权益见各策略 JSON。当前完整股池见 current-pool.csv。",
    ]
    (directory / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload
