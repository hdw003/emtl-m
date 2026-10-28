"""Daily long-only quant research, replayable paper trading, and opt-in execution."""

from .config import QuantConfig, load_config
from .data import EastmoneyDailyProvider, load_csv, write_csv
from .engine import Backtester, BacktestResult
from .execution import EMTBroker
from .models import Bar, Costs, Instrument, OrderIntent
from .paper import run_paper
from .planning import AccountSnapshot, plan_rebalance
from .risk import RiskPolicy
from .strategies import STRATEGIES, StrategySpec

__all__ = [
    "STRATEGIES",
    "AccountSnapshot",
    "BacktestResult",
    "Backtester",
    "Bar",
    "Costs",
    "EMTBroker",
    "EastmoneyDailyProvider",
    "Instrument",
    "OrderIntent",
    "QuantConfig",
    "RiskPolicy",
    "StrategySpec",
    "load_config",
    "load_csv",
    "plan_rebalance",
    "run_paper",
    "write_csv",
]
