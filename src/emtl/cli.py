"""命令行入口：登录东财网页交易并执行查询/下单/撤单。

用法示例：
    emt price 600000                       # 只要行情，无需登录
    emt query asset                        # 需登录（账号取 -u/-p 或环境变量）
    emt query orders
    emt query trades
    emt buy 600000 10.21 100 --market SH
    emt sell 600000 10.21 100 --market SH
    emt cancel 20240520_130662
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from . import __version__
from ._client import EMTClient
from ._models import ApiResponse, Direction, TradeMarket, date_range, guess_market
from .errors import EMTError


def _print(payload: ApiResponse | str) -> None:
    if isinstance(payload, str):
        print(payload)
        return
    print(json.dumps(payload.json, ensure_ascii=False, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="emt", description="东方财富证券网页交易客户端")
    p.add_argument("-u", "--user", help="用户名；缺省取环境变量 EM_USERNAME")
    p.add_argument("-p", "--password", help="密码；缺省取环境变量 EM_PASSWORD")
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    q = sub.add_parser("query", help="查询账户数据")
    q.add_argument("kind", choices=["asset", "orders", "trades", "funds"])
    q.add_argument("--days", type=int, default=30, help="历史查询窗口天数（funds 需要）")

    price = sub.add_parser("price", help="实时价格（无需登录）")
    price.add_argument("code")
    price.add_argument("--market", choices=["SH", "SZ"], help="缺省按代码前缀推断")

    for name, target in (("buy", Direction.BUY), ("sell", Direction.SELL)):
        act = sub.add_parser(name, help=f"{target.name} 股票")
        act.add_argument("code")
        act.add_argument("price", type=float)
        act.add_argument("qty", type=int)
        act.add_argument("--market", choices=["SH", "SZ"], help="缺省按代码前缀推断")

    cancel = sub.add_parser("cancel", help="撤单")
    cancel.add_argument("order", help="成交日期_成交编号，如 20240520_130662")

    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        client = EMTClient(username=args.user, password=args.password)

        if args.command == "price":
            market = TradeMarket(args.market) if args.market else guess_market(args.code)
            v = client.last_price(args.code, market.value)
            print(v if v == v else "nan")  # NaN 自比较为 False
            return 0

        client.login()
        match args.command:
            case "query":
                if args.kind == "asset":
                    _print(client.query_asset_and_position())
                elif args.kind == "orders":
                    _print(client.query_orders())
                elif args.kind == "trades":
                    _print(client.query_trades())
                else:
                    st, et = date_range(args.days)
                    _print(client.query_funds_flow(100, st, et))
            case "buy" | "sell":
                direction = Direction.BUY if args.command == "buy" else Direction.SELL
                market = TradeMarket(args.market) if args.market else guess_market(args.code)
                _print(client.create_order(args.code, direction, market, args.price, args.qty))
            case "cancel":
                print(client.cancel_order(args.order))
            case _:
                _build_parser().print_help(sys.stderr)
                return 2
        return 0
    except EMTError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
