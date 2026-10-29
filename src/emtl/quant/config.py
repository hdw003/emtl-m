"""Strict TOML configuration shared by backtest and paper commands."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from .engine import Backtester
from .market import CatalogHistory, MarketSpec, MarketStore
from .models import Costs, Instrument
from .portfolio import PortfolioSpec
from .risk import RiskPolicy
from .strategies import StrategySpec
from .value import Fundamental, ValueSpec, ValueStore, ValueUniverse


@dataclass(frozen=True, slots=True)
class QuantConfig:
    data: Path
    instruments: tuple[Instrument, ...]
    strategy: StrategySpec
    costs: Costs
    risk: RiskPolicy
    initial_cash: float = 1_000_000
    rebalance: str = "monthly"
    start: dt.date | None = None
    end: dt.date | None = None
    value: ValueSpec | None = None
    data_provider: str = "eastmoney"
    market: MarketSpec | None = None
    portfolio: PortfolioSpec | None = None

    def engine(
        self,
        *,
        value_facts: list[Fundamental] | None = None,
        selection_ranking: dict[str, float] | None = None,
    ) -> Backtester:
        if self.market and not self.instruments:
            raise ValueError("full-market catalog is not published; run quant market-scan first")
        if self.portfolio and self.value is None and selection_ranking is None:
            raise ValueError(
                "[portfolio] needs [value] or an explicitly labeled frozen research ranking"
            )
        return Backtester(
            self.instruments,
            self.strategy,
            initial_cash=self.initial_cash,
            costs=self.costs,
            risk=self.risk,
            rebalance=self.rebalance,
            universe=ValueUniverse(
                self.value,
                value_facts
                if value_facts is not None
                else ValueStore(self.value.fundamentals).load(),
                CatalogHistory(
                    MarketStore(self.market.store).catalogs(),
                    MarketStore(self.market.store).history_availability(),
                ).members
                if self.market
                else None,
            )
            if self.value
            else None,
            fractional_action=self.market.fractional_action if self.market else "error",
            allocation=self.portfolio,
            selection_ranking=selection_ranking,
        )

    def fingerprint(self) -> str:
        content = asdict(self)
        content["engine_model_version"] = 1
        content.pop("data")  # Moving a byte-identical dataset does not change its semantics.
        if self.portfolio is None:
            content.pop("portfolio")
        else:
            content["portfolio_model_version"] = 1
        if self.market is None:
            content.pop("market")
        else:
            content.pop("instruments")
            for key in ("store", "workers", "page_size"):
                content["market"].pop(key)
            content["market_rule_version"] = 1
        if self.value is None:
            content.pop("value")  # Keep pre-value ETF paper ledgers compatible.
        else:
            content["value"].pop("fundamentals")
            content["value_model_version"] = 1
        if self.data_provider == "eastmoney":
            content.pop("data_provider")
        return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()


def load_config(path: Path) -> QuantConfig:
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    allowed = {
        "data",
        "instruments",
        "strategy",
        "costs",
        "risk",
        "initial_cash",
        "rebalance",
        "start",
        "end",
        "value",
        "data_provider",
        "market",
        "portfolio",
    }
    if set(raw) - allowed:
        raise ValueError(f"unknown config keys: {sorted(set(raw) - allowed)}")
    try:
        data = Path(raw["data"])
        market_raw = dict(raw["market"]) if "market" in raw else None
        if market_raw is not None:
            if raw.get("instruments"):
                raise ValueError(
                    "full-market mode discovers its own instruments; remove fixed [[instruments]]"
                )
            market_raw["store"] = (path.parent / market_raw["store"]).resolve()
            if "history_start" in market_raw:
                market_raw["history_start"] = dt.date.fromisoformat(
                    str(market_raw["history_start"])
                )
        market = MarketSpec(**market_raw) if market_raw is not None else None
        value_raw = dict(raw["value"]) if "value" in raw else None
        if value_raw is not None:
            value_raw["fundamentals"] = (path.parent / value_raw["fundamentals"]).resolve()
            if "excluded_industries" in value_raw:
                value_raw["excluded_industries"] = tuple(value_raw["excluded_industries"])
            for key in ("top_n", "max_per_industry"):
                if value_raw.get(key) == "all":
                    value_raw[key] = None
        config = QuantConfig(
            data=(path.parent / data).resolve(),
            instruments=MarketStore(market.store).instruments()
            if market
            else tuple(Instrument(**item) for item in raw["instruments"]),
            strategy=StrategySpec(**raw.get("strategy", {})),
            costs=Costs(**raw.get("costs", {})),
            risk=RiskPolicy(**raw.get("risk", {})),
            value=ValueSpec(**value_raw) if value_raw is not None else None,
            data_provider=raw.get("data_provider", "eastmoney"),
            market=market,
            portfolio=PortfolioSpec(**raw["portfolio"]) if "portfolio" in raw else None,
            **{key: raw[key] for key in ("initial_cash", "rebalance") if key in raw},
            **{key: dt.date.fromisoformat(str(raw[key])) for key in ("start", "end") if key in raw},
        )
        if config.instruments:
            config.engine()
        elif not config.market:
            raise ValueError("instrument universe must be nonempty")
        if config.market and config.value is None:
            raise ValueError("full-market mode requires [value]")
        if config.data_provider not in {"eastmoney", "tencent"}:
            raise ValueError("data_provider must be eastmoney or tencent")
        if config.start and config.end and config.start > config.end:
            raise ValueError("start must be <= end")
        return config
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid quant config: {exc}") from exc
