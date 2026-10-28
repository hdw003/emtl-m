"""Portfolio limits applied after strategy signals, without leverage or shorts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import stdev

from .strategies import History


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    max_position: float = 0.35
    max_gross: float = 0.95
    max_drawdown: float = 0.20
    stop_loss: float = 0.10
    trailing_stop: float = 0.15
    cooldown: int = 20
    target_volatility: float | None = None
    volatility_window: int = 60

    def __post_init__(self) -> None:
        for name in ("max_position", "max_gross", "max_drawdown", "stop_loss", "trailing_stop"):
            if not 0 < getattr(self, name) <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        if type(self.cooldown) is not int or self.cooldown < 1 or self.volatility_window < 2:
            raise ValueError("cooldown >= 1 and volatility_window >= 2 required")
        if self.target_volatility is not None and (
            not math.isfinite(self.target_volatility) or self.target_volatility <= 0
        ):
            raise ValueError("target_volatility must be positive")

    def constrain(self, weights: dict[str, float], history: History) -> dict[str, float]:
        if any(not math.isfinite(w) or w < 0 for w in weights.values()):
            raise ValueError("strategy weights must be finite and long-only")
        result = {s: min(w, self.max_position) for s, w in weights.items() if w > 0}
        gross = sum(result.values())
        scale = min(1, self.max_gross / gross) if gross else 1
        result = {s: w * scale for s, w in result.items()}
        if self.target_volatility is not None and result:
            window = self.volatility_window
            if any(len(history[s]) <= window for s in result):
                return {}
            returns = [
                sum(
                    w * (history[s][-window + i] / history[s][-window + i - 1] - 1)
                    for s, w in result.items()
                )
                for i in range(window)
            ]
            volatility = stdev(returns) * math.sqrt(252)
            scale = min(1, self.target_volatility / volatility) if volatility else 1
            result = {s: w * scale for s, w in result.items()}
        return result
