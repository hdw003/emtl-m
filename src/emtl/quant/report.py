"""Machine-readable reports with the assumptions needed to interpret returns."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from .config import QuantConfig
from .engine import BacktestResult


def report(result: BacktestResult, config: QuantConfig) -> dict:
    return {
        "schema_version": 1,
        "config_hash": config.fingerprint(),
        "strategy": asdict(config.strategy),
        "metrics": result.metrics(),
        "halted": result.halted,
        "next_weights": result.next_weights,
        "positions": {s: asdict(p) for s, p in result.portfolio.positions.items() if p.quantity},
        "equity_curve": [{**asdict(p), "date": str(p.date)} for p in result.curve],
        "fills": [f.as_dict() for f in result.fills],
        "events": result.events,
        "assumptions": {
            "data_quality": "not independently certified; corporate actions and historical universe must be verified",
            "signal": "completed daily close; execute next observed session open",
            "price_basis": "raw OHLC with explicit ex-date splits and cash dividends",
            "missing_bars": "carry valuation; no execution without a bar",
            "limits": "conservative opening limit lock; no intraday queue model",
            "liquidity": "previous observed daily share volume participation cap",
            "unfilled": "unfilled target is discarded; reconsider at next scheduled signal or risk exit",
            "costs": asdict(config.costs),
            "risk": asdict(config.risk),
            "universe": "user-supplied; survivorship bias must be assessed separately",
            "benchmark": "none; absolute return metrics, zero-rate Sharpe",
        },
    }


def write_json(path: Path, payload: dict) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".report-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)
