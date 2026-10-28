"""Long-only daily strategies. Inputs contain only closes known at signal time."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from statistics import mean, stdev
from typing import Protocol

History = Mapping[str, Sequence[float]]
STRATEGIES = (
    "buy_hold",
    "dual_ma",
    "time_series_momentum",
    "momentum_rotation",
    "donchian",
    "bollinger",
    "inverse_volatility",
)


class Strategy(Protocol):
    def weights(self, history: History, held: Mapping[str, float]) -> dict[str, float]: ...


def _equal(symbols: Sequence[str]) -> dict[str, float]:
    return {s: 1 / len(symbols) for s in symbols} if symbols else {}


@dataclass(frozen=True, slots=True)
class StrategySpec:
    name: str = "momentum_rotation"
    fast: int = 50
    slow: int = 200
    lookback: int = 126
    skip: int = 0
    top_n: int = 3
    exit_window: int = 20
    entry_z: float = 2.0
    exit_z: float = 0.0
    absolute_filter: bool = True

    def __post_init__(self) -> None:
        if self.name not in STRATEGIES:
            raise ValueError(f"unknown strategy {self.name}; choose from {STRATEGIES}")
        for name in ("fast", "slow", "lookback", "top_n", "exit_window"):
            if type(getattr(self, name)) is not int or getattr(self, name) < (
                1 if name == "top_n" else 2
            ):
                raise ValueError(f"{name} must be an integer >= 2 (top_n >= 1)")
        if self.fast >= self.slow or type(self.skip) is not int or self.skip < 0:
            raise ValueError("fast < slow and skip >= 0 required")
        if (
            not math.isfinite(self.entry_z)
            or not math.isfinite(self.exit_z)
            or not 0 <= self.exit_z < self.entry_z
        ):
            raise ValueError("0 <= exit_z < entry_z required")

    def weights(self, history: History, held: Mapping[str, float]) -> dict[str, float]:
        selected: list[str] = []
        scores: dict[str, float] = {}
        for symbol, values in sorted(history.items()):
            if not values:
                continue
            match self.name:
                case "buy_hold":
                    selected.append(symbol)
                case "dual_ma":
                    if len(values) >= self.slow and mean(values[-self.fast :]) > mean(
                        values[-self.slow :]
                    ):
                        selected.append(symbol)
                case "time_series_momentum" | "momentum_rotation":
                    if len(values) > self.lookback + self.skip:
                        score = values[-1 - self.skip] / values[-1 - self.skip - self.lookback] - 1
                        if not self.absolute_filter or score > 0:
                            scores[symbol] = score
                case "donchian":
                    if len(values) > max(self.lookback, self.exit_window):
                        holding = held.get(symbol, 0) > 0
                        if (holding and values[-1] >= min(values[-self.exit_window - 1 : -1])) or (
                            not holding and values[-1] > max(values[-self.lookback - 1 : -1])
                        ):
                            selected.append(symbol)
                case "bollinger":
                    if len(values) >= self.lookback:
                        window = values[-self.lookback :]
                        deviation = stdev(window)
                        if deviation > 0:
                            z = (values[-1] - mean(window)) / deviation
                            if z <= -self.entry_z or (held.get(symbol, 0) > 0 and z < self.exit_z):
                                selected.append(symbol)
                case "inverse_volatility":
                    if len(values) > self.lookback:
                        window = values[-self.lookback - 1 :]
                        volatility = stdev([b / a - 1 for a, b in pairwise(window)])
                        if volatility > 1e-12:
                            scores[symbol] = 1 / volatility
        if self.name == "momentum_rotation":
            return _equal(sorted(scores, key=lambda s: (-scores[s], s))[: self.top_n])
        if self.name == "time_series_momentum":
            return _equal(list(scores))
        if self.name == "inverse_volatility":
            total = sum(scores.values())
            return {s: value / total for s, value in scores.items()} if total else {}
        return _equal(selected)
