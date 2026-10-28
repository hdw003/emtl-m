"""Explicit, journaled bridge to EMTClient. Never invoked by backtest or paper."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from emtl._client import EMTClient
from emtl._models import ApiResponse, Direction, TradeMarket

from .models import OrderIntent, finite


class EMTBroker:
    """Opt-in limit-order submission with conservative ambiguous-outcome handling.

    This adapter does not infer account fields or assume submitted means filled.
    A request recorded as submitting/unknown must be reconciled with the broker
    manually, never automatically retried. Use one journal per account.
    """

    def __init__(
        self,
        client: EMTClient,
        journal: Path,
        *,
        enabled: bool = False,
        max_order_value: float = 10_000,
    ) -> None:
        finite(max_order_value, "max_order_value", minimum=1e-12)
        self.client, self.journal = client, journal
        self.enabled, self.max_order_value = enabled, max_order_value

    def submit(self, intent: OrderIntent, request_id: str) -> ApiResponse:
        if not self.enabled:
            raise RuntimeError(
                "live execution is disabled; enable explicitly after account reconciliation"
            )
        if intent.symbol.endswith(".BJ"):
            raise ValueError("BJ live routing is not supported by EMTClient; research/paper only")
        if not request_id or len(request_id) > 128:
            raise ValueError("a stable request_id of 1..128 characters is required")
        if intent.quantity * intent.limit_price > self.max_order_value:
            raise ValueError("order exceeds max_order_value")
        if intent.side == "buy" and intent.quantity % 100:
            raise ValueError("buy quantity must be a multiple of 100 for supported A shares/ETFs")
        if not self.client.validate_key:
            raise RuntimeError("authenticate explicitly before live submission")
        fingerprint = hashlib.sha256(
            json.dumps(asdict(intent), sort_keys=True).encode()
        ).hexdigest()
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.journal, timeout=30) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS submissions (request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL, intent TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT fingerprint, state FROM submissions WHERE request_id=?", (request_id,)
            ).fetchone()
            if previous:
                raise RuntimeError(
                    f"request_id already recorded ({previous[1]}); reconcile before any new order"
                )
            connection.execute(
                "INSERT INTO submissions VALUES (?, ?, 'submitting', ?, ?, ?)",
                (
                    request_id,
                    fingerprint,
                    json.dumps(asdict(intent), sort_keys=True),
                    dt.datetime.now(dt.UTC).isoformat(),
                    dt.datetime.now(dt.UTC).isoformat(),
                ),
            )
        try:
            _, market = intent.symbol.split(".")
            response = self.client.create_order(
                intent.symbol.split(".")[0],
                Direction.BUY if intent.side == "buy" else Direction.SELL,
                TradeMarket[market],
                intent.limit_price,
                intent.quantity,
            )
            response.ensure_ok()
        except Exception:
            # Includes timeouts after server acceptance and business errors.
            # Never silently re-send an order with unknown execution outcome.
            self._state(request_id, "unknown")
            raise
        self._state(request_id, "submitted")
        return response

    def _state(self, request_id: str, state: str) -> None:
        with sqlite3.connect(self.journal) as connection:
            connection.execute(
                "UPDATE submissions SET state=?, updated_at=? WHERE request_id=?",
                (state, dt.datetime.now(dt.UTC).isoformat(), request_id),
            )
