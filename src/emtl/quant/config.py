"""Strict TOML configuration shared by backtest and paper commands."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from .engine import Backtester
from .models import Costs, Instrument
from .risk import RiskPolicy
from .strategies import StrategySpec


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

    def engine(self) -> Backtester:
        return Backtester(
            self.instruments,
            self.strategy,
            initial_cash=self.initial_cash,
            costs=self.costs,
            risk=self.risk,
            rebalance=self.rebalance,
        )

    def fingerprint(self) -> str:
        content = asdict(self)
        content["engine_model_version"] = 1
        content.pop("data")  # Moving a byte-identical dataset does not change its semantics.
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
    }
    if set(raw) - allowed:
        raise ValueError(f"unknown config keys: {sorted(set(raw) - allowed)}")
    try:
        data = Path(raw["data"])
        config = QuantConfig(
            data=(path.parent / data).resolve(),
            instruments=tuple(Instrument(**item) for item in raw["instruments"]),
            strategy=StrategySpec(**raw.get("strategy", {})),
            costs=Costs(**raw.get("costs", {})),
            risk=RiskPolicy(**raw.get("risk", {})),
            **{key: raw[key] for key in ("initial_cash", "rebalance") if key in raw},
            **{key: dt.date.fromisoformat(str(raw[key])) for key in ("start", "end") if key in raw},
        )
        config.engine()  # Validate even when the caller only wants to fetch data.
        if config.start and config.end and config.start > config.end:
            raise ValueError("start must be <= end")
        return config
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid quant config: {exc}") from exc
