"""Quant CLI routes are isolated from account login and trading commands."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .config import QuantConfig, load_config
from .data import EastmoneyDailyProvider, TencentDailyProvider, load_csv, write_csv
from .demo import create_demo, create_value_demo
from .paper import latest_completed_date, run_paper
from .report import report, write_json
from .strategies import STRATEGIES, StrategySpec
from .value import ValueStore
from .value_ops import capture_value, inspect_pool


def add_quant_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "quant", help="日线策略回测、模拟盘和数据管理（不登录、不实盘下单）"
    )
    commands = parser.add_subparsers(dest="quant_command", required=True)
    commands.add_parser("strategies", help="列出内置策略")
    demo = commands.add_parser("demo", help="生成可离线运行的合成数据和示例配置")
    demo.add_argument("--directory", type=Path, required=True)
    value_demo = commands.add_parser("value-demo", help="生成价值股池与长线策略结合的离线合成示例")
    value_demo.add_argument("--directory", type=Path, required=True)
    for command in ("value-capture", "value-import", "value-pool"):
        value_parser = commands.add_parser(command, help="价值年报采集、时点数据导入或股池预览")
        value_parser.add_argument("--config", type=Path, required=True)
        if command == "value-capture":
            value_parser.add_argument("--force", action="store_true")
        elif command == "value-import":
            value_parser.add_argument("--input", type=Path, required=True)
        else:
            value_parser.add_argument("--asof", type=dt.date.fromisoformat)
            value_parser.add_argument("--output", type=Path, required=True)
    for command in ("backtest", "paper", "fetch", "daily"):
        p = commands.add_parser(command)
        p.add_argument("--config", type=Path, required=True)
        if command in {"fetch", "daily"}:
            p.add_argument("--start", type=dt.date.fromisoformat, required=True)
        if command == "fetch":
            p.add_argument("--end", type=dt.date.fromisoformat, required=True)
            p.add_argument("--append", action="store_true", help="只追加新日期，保留历史和公司行动")
        else:
            p.add_argument("--output", type=Path, required=True)
        if command == "backtest":
            p.add_argument(
                "--strategy", choices=STRATEGIES, help="仅覆盖策略名称，其他参数来自配置"
            )
        if command in {"paper", "daily"}:
            p.add_argument("--database", type=Path, required=True)
            p.add_argument("--asof", type=dt.date.fromisoformat)


def _fetch(config: QuantConfig, start: dt.date, end: dt.date, *, append: bool) -> dict:
    if start > end:
        raise ValueError("start must be <= end")
    if end > latest_completed_date():
        raise ValueError("fetch requires completed daily bars (after 16:00 Asia/Shanghai)")
    if config.data.exists() and not append:
        raise ValueError("data already exists; use --append or choose a new data path")
    existing = load_csv(config.data) if config.data.exists() else []
    if {b.symbol for b in existing} - {i.symbol for i in config.instruments}:
        raise ValueError("existing CSV contains symbols outside the configured universe")
    provider = (
        TencentDailyProvider() if config.data_provider == "tencent" else EastmoneyDailyProvider()
    )
    appended = []
    for instrument in config.instruments:
        dates = [b.date for b in existing if b.symbol == instrument.symbol]
        beginning = max(start, max(dates) + dt.timedelta(days=1)) if dates else start
        if beginning <= end:
            appended.extend(provider.fetch(instrument, beginning, end))
    if not existing and not appended:
        raise ValueError("no bars downloaded")
    write_csv(config.data, [*existing, *appended])
    return {
        "data": str(config.data),
        "data_provider": config.data_provider,
        "appended_bars": len(appended),
        "corporate_actions": "not supplied; annotate raw data before performance research",
    }


def run_quant(args: argparse.Namespace) -> int:
    try:
        if args.quant_command == "strategies":
            print(json.dumps(list(STRATEGIES), ensure_ascii=False, indent=2))
            return 0
        if args.quant_command == "demo":
            print(create_demo(args.directory))
            return 0
        if args.quant_command == "value-demo":
            print(create_value_demo(args.directory))
            return 0
        config = load_config(args.config)
        if args.quant_command.startswith("value-"):
            if config.value is None:
                raise ValueError("configure [value] before using value commands")
            if args.quant_command == "value-capture":
                payload = capture_value(config, force=args.force)
            elif args.quant_command == "value-import":
                payload = {"inserted": ValueStore(config.value.fundamentals).import_csv(args.input)}
            else:
                payload = inspect_pool(
                    config, args.asof or dt.datetime.now(ZoneInfo("Asia/Shanghai")).date()
                )
                write_json(args.output, payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        if args.quant_command == "fetch":
            print(json.dumps(_fetch(config, args.start, args.end, append=args.append)))
            return 0
        if args.quant_command == "daily":
            if config.value is not None:
                capture_value(config)
            _fetch(config, args.start, args.asof or latest_completed_date(), append=True)
        if args.quant_command in {"paper", "daily"}:
            result, new_sessions = run_paper(config, args.database, asof=args.asof)
        else:
            engine = config.engine()
            if args.strategy:
                config = replace(config, strategy=replace(config.strategy, name=args.strategy))
                engine = config.engine()
            result = engine.run(load_csv(config.data), start=config.start, end=config.end)
            new_sessions = None
        payload = report(result, config)
        if new_sessions is not None:
            payload["paper"] = {"new_sessions": new_sessions, "database": str(args.database)}
            if config.value is not None:
                store = ValueStore(config.value.fundamentals)
                for snapshot in result.universe_history:
                    store.save_pool(snapshot)
        # Equal-universe buy-and-hold under the same costs, cash and risk limits.
        if args.quant_command == "backtest" and config.strategy.name != "buy_hold":
            baseline = config.engine()
            baseline.strategy = StrategySpec(name="buy_hold")
            payload["reference_buy_hold_same_risk"] = baseline.run(
                load_csv(config.data), start=config.start, end=config.end
            ).metrics()
        write_json(args.output, payload)
        print(
            json.dumps(
                {
                    "output": str(args.output),
                    "metrics": payload["metrics"],
                    "halted": result.halted,
                    "next_weights": result.next_weights,
                    "new_sessions": new_sessions,
                    "value_status": result.value_status,
                },
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
        )
        return 0
    except (ValueError, OSError, sqlite3.Error, requests.RequestException) as exc:
        print(f"量化运行失败: {exc}", file=sys.stderr)
        return 1
