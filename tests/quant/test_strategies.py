"""Hand-computed signals, warm-up boundaries and portfolio limits."""

import math

import pytest

from emtl.quant import RiskPolicy, StrategySpec


@pytest.mark.parametrize(
    "name",
    [
        "dual_ma",
        "time_series_momentum",
        "momentum_rotation",
        "donchian",
        "bollinger",
        "inverse_volatility",
    ],
)
def test_no_signal_before_warmup(name: str) -> None:
    assert StrategySpec(name=name).weights({"a": [10]}, {}) == {}


def test_dual_ma_and_absolute_momentum() -> None:
    data = {"up": [1, 2, 3, 4], "down": [4, 3, 2, 1]}
    assert StrategySpec(name="dual_ma", fast=2, slow=4).weights(data, {}) == {"up": 1}
    assert StrategySpec(name="time_series_momentum", lookback=3).weights(data, {}) == {"up": 1}


def test_rotation_ranking_skip_and_tie_break() -> None:
    spec = StrategySpec(name="momentum_rotation", lookback=2, skip=1, top_n=1)
    data = {"b": [1, 2, 3, 0.5], "a": [1, 2, 3, 10], "c": [3, 2, 1, 100]}
    assert spec.weights(data, {}) == {"a": 1}
    assert spec.weights({"negative": [3, 2, 1, 10]}, {}) == {}


def test_donchian_excludes_current_bar_and_retains_until_exit() -> None:
    spec = StrategySpec(name="donchian", lookback=3, exit_window=2)
    assert spec.weights({"a": [1, 2, 3, 4]}, {}) == {"a": 1}
    assert spec.weights({"a": [1, 2, 4, 3]}, {}) == {}
    assert spec.weights({"a": [1, 2, 4, 3]}, {"a": 1}) == {"a": 1}
    assert spec.weights({"a": [1, 3, 4, 2]}, {"a": 1}) == {}


def test_bollinger_entry_hold_and_exit() -> None:
    spec = StrategySpec(name="bollinger", lookback=4, entry_z=1, exit_z=0)
    assert spec.weights({"a": [10, 10, 10, 5]}, {}) == {"a": 1}
    assert spec.weights({"a": [10, 10, 5, 8]}, {"a": 1}) == {"a": 1}
    assert spec.weights({"a": [10, 5, 8, 12]}, {"a": 1}) == {}


def test_inverse_volatility_is_not_equal_risk_contribution() -> None:
    spec = StrategySpec(name="inverse_volatility", lookback=3)
    data = {"a": [100, 110, 99, 108.9], "b": [100, 120, 96, 115.2], "flat": [10] * 4}
    weights = spec.weights(data, {})
    assert weights == pytest.approx({"a": 2 / 3, "b": 1 / 3})


def test_risk_caps_cash_and_target_volatility() -> None:
    risk = RiskPolicy(max_position=0.5, max_gross=0.8)
    assert risk.constrain({"a": 0.9, "b": 0.9}, {}) == {"a": 0.4, "b": 0.4}
    with pytest.raises(ValueError, match="finite and long-only"):
        risk.constrain({"a": math.nan}, {})
    risk = RiskPolicy(max_position=1, target_volatility=0.01, volatility_window=3)
    assert risk.constrain({"a": 1}, {"a": [10]}) == {}
    weights = risk.constrain({"a": 1}, {"a": [10, 11, 9, 12]})
    assert 0 < weights["a"] < 0.01


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "unknown"},
        {"top_n": 0},
        {"lookback": True},
        {"skip": -1},
        {"fast": 200},
        {"entry_z": math.nan},
    ],
)
def test_bad_strategy_configuration(kwargs: dict) -> None:
    with pytest.raises(ValueError, match=r"strategy|integer|skip|entry_z"):
        StrategySpec(**kwargs)
