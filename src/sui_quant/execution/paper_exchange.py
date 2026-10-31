"""PaperOrderbookSimulator: Pure Local Memory + SQLite WAL Simulated Exchange for P4 (§5, §7, §8, §16).

Physical Isolation Guarantees (INV-R1):
- Zero live Binance REST/WebSocket order placement code or network sockets.
- Strictly enforces `ExecutionMode.PAPER` and `symbol == "SUIUSDT"`.
- Supports deterministic fill simulation (0% never-fill, 40% partial fill, micro-fill < minNotional,
  100% full fill), multi-asset fee deduction (`SUI`, `USDT`, `BNB`), idempotent `clientOrderId`
  reconciliation (`query_order_by_client_order_id`), and SQLite WAL persistence across crash restarts.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from pathlib import Path

from sui_quant.config import ExecutionMode
from sui_quant.contracts import (
    TRADING_SYMBOL,
    OrderEvent,
    OrderIntent,
    OrderSide,
    TimeInForce,
    require_decimal,
)
from sui_quant.data.quality_guard import PartialOrderbookSnapshot
from sui_quant.exchange_rules import P0_SNAPSHOT_SUIUSDT_FILTERS, SymbolFilters
from sui_quant.utils.clock import ensure_utc, utc_now


class ExchangeTimeoutError(TimeoutError):
    """Raised when an order submission or cancellation times out before receiving a response (QA-04)."""


class OrderStatus(str, Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True)
class OrderSnapshot:
    """Authoritative simulated exchange order state returned by `query_order_by_client_order_id`."""

    client_order_id: str
    exchange_order_id: int | None
    symbol: str
    side: OrderSide
    order_type: str  # "LIMIT", "STOP_LOSS_LIMIT", "MARKET_EXIT"
    status: OrderStatus
    price: Decimal
    stop_price: Decimal | None
    orig_qty: Decimal
    filled_qty: Decimal
    cumulative_quote_qty: Decimal
    avg_fill_price: Decimal
    fee_asset: str
    fee_amount: Decimal
    trade_ids: tuple[int, ...]
    created_at_utc: datetime
    updated_at_utc: datetime

    def __post_init__(self) -> None:
        ensure_utc(self.created_at_utc, "created_at_utc")
        ensure_utc(self.updated_at_utc, "updated_at_utc")
        for name, val in (
            ("price", self.price),
            ("orig_qty", self.orig_qty),
            ("filled_qty", self.filled_qty),
            ("cumulative_quote_qty", self.cumulative_quote_qty),
            ("avg_fill_price", self.avg_fill_price),
            ("fee_amount", self.fee_amount),
        ):
            require_decimal(val, name)
        if self.stop_price is not None:
            require_decimal(self.stop_price, "stop_price")

    @property
    def remaining_qty(self) -> Decimal:
        return max(Decimal("0"), self.orig_qty - self.filled_qty)

    def to_order_event(self) -> OrderEvent:
        return OrderEvent(
            client_order_id=self.client_order_id,
            exchange_status=self.status.value,
            filled_qty=self.filled_qty,
            cumulative_quote_qty=self.cumulative_quote_qty,
            fee_asset=self.fee_asset,
            fee_amount=self.fee_amount,
            trade_ids=self.trade_ids,
            source_timestamp=self.updated_at_utc,
            exchange_order_id=self.exchange_order_id,
        )


class PaperOrderbookSimulator:
    """Deterministic PAPER orderbook matching engine with SQLite WAL crash persistence (§3.7)."""

    def __init__(
        self,
        *,
        initial_usdt: Decimal = Decimal("10000.0000"),
        initial_sui: Decimal = Decimal("0.0"),
        initial_bnb: Decimal = Decimal("10.0"),
        commission_asset: str = "SUI",
        buy_fee_rate: Decimal = Decimal("0.0010"),
        sell_fee_rate: Decimal = Decimal("0.0010"),
        filters: SymbolFilters = P0_SNAPSHOT_SUIUSDT_FILTERS,
        execution_mode: ExecutionMode = ExecutionMode.PAPER,
        db_path: str | Path | None = None,
    ) -> None:
        if execution_mode != ExecutionMode.PAPER:
            raise PermissionError(
                f"PaperOrderbookSimulator strictly requires ExecutionMode.PAPER, got {execution_mode}"
            )
        if commission_asset.upper() not in {"SUI", "USDT", "BNB"}:
            raise ValueError(f"Unsupported commission_asset: {commission_asset}")
        self.execution_mode = execution_mode
        self.commission_asset = commission_asset.upper()
        self.buy_fee_rate = require_decimal(buy_fee_rate, "buy_fee_rate")
        self.sell_fee_rate = require_decimal(sell_fee_rate, "sell_fee_rate")
        self.filters = filters

        self._free_usdt = require_decimal(initial_usdt, "initial_usdt")
        self._locked_usdt = Decimal("0.0000")
        self._free_sui = require_decimal(initial_sui, "initial_sui")
        self._locked_sui = Decimal("0.0")
        self._free_bnb = require_decimal(initial_bnb, "initial_bnb")

        self._orders: dict[str, OrderSnapshot] = {}
        self._next_order_id = 100_001
        self._next_trade_id = 500_001
        self._live_orders_sent_count = 0

        # Fault-injection hooks for QA-04 / FI-P4-05
        self._injected_submit_timeout_mode: str | None = None
        self._injected_submit_partial_ratio: Decimal | None = None

        self._db_path = Path(db_path) if db_path is not None else None
        self._conn: sqlite3.Connection | None = None
        if self._db_path is not None:
            self._init_persistence()

    def _init_persistence(self) -> None:
        assert self._db_path is not None
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), timeout=5.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_exchange_balances (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                free_usdt TEXT NOT NULL,
                locked_usdt TEXT NOT NULL,
                free_sui TEXT NOT NULL,
                locked_sui TEXT NOT NULL,
                free_bnb TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_exchange_orders (
                client_order_id TEXT PRIMARY KEY,
                exchange_order_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                status TEXT NOT NULL,
                price TEXT NOT NULL,
                stop_price TEXT,
                orig_qty TEXT NOT NULL,
                filled_qty TEXT NOT NULL,
                cumulative_quote_qty TEXT NOT NULL,
                avg_fill_price TEXT NOT NULL,
                fee_asset TEXT NOT NULL,
                fee_amount TEXT NOT NULL,
                trade_ids_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );
            """
        )
        row = self._conn.execute("SELECT * FROM paper_exchange_balances WHERE id = 1").fetchone()
        if row is not None:
            self._free_usdt = Decimal(str(row["free_usdt"]))
            self._locked_usdt = Decimal(str(row["locked_usdt"]))
            self._free_sui = Decimal(str(row["free_sui"]))
            self._locked_sui = Decimal(str(row["locked_sui"]))
            self._free_bnb = Decimal(str(row["free_bnb"]))
        else:
            self._persist_balances()

        order_rows = self._conn.execute("SELECT * FROM paper_exchange_orders").fetchall()
        for r in order_rows:
            snap = OrderSnapshot(
                client_order_id=str(r["client_order_id"]),
                exchange_order_id=int(r["exchange_order_id"]),
                symbol=str(r["symbol"]),
                side=OrderSide(str(r["side"])),
                order_type=str(r["order_type"]),
                status=OrderStatus(str(r["status"])),
                price=Decimal(str(r["price"])),
                stop_price=Decimal(str(r["stop_price"])) if r["stop_price"] is not None else None,
                orig_qty=Decimal(str(r["orig_qty"])),
                filled_qty=Decimal(str(r["filled_qty"])),
                cumulative_quote_qty=Decimal(str(r["cumulative_quote_qty"])),
                avg_fill_price=Decimal(str(r["avg_fill_price"])),
                fee_asset=str(r["fee_asset"]),
                fee_amount=Decimal(str(r["fee_amount"])),
                trade_ids=tuple(json.loads(str(r["trade_ids_json"]))),
                created_at_utc=datetime.fromisoformat(str(r["created_at_utc"])),
                updated_at_utc=datetime.fromisoformat(str(r["updated_at_utc"])),
            )
            self._orders[snap.client_order_id] = snap
            if snap.exchange_order_id is not None and snap.exchange_order_id >= self._next_order_id:
                self._next_order_id = snap.exchange_order_id + 1

    def _persist_balances(self) -> None:
        if self._conn is None:
            return
        self._conn.execute(
            """
            INSERT INTO paper_exchange_balances (id, free_usdt, locked_usdt, free_sui, locked_sui, free_bnb)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                free_usdt=excluded.free_usdt,
                locked_usdt=excluded.locked_usdt,
                free_sui=excluded.free_sui,
                locked_sui=excluded.locked_sui,
                free_bnb=excluded.free_bnb
            """,
            (
                str(self._free_usdt),
                str(self._locked_usdt),
                str(self._free_sui),
                str(self._locked_sui),
                str(self._free_bnb),
            ),
        )

    def _persist_order(self, snap: OrderSnapshot) -> None:
        self._orders[snap.client_order_id] = snap
        if self._conn is None:
            return
        self._conn.execute(
            """
            INSERT INTO paper_exchange_orders (
                client_order_id, exchange_order_id, symbol, side, order_type, status,
                price, stop_price, orig_qty, filled_qty, cumulative_quote_qty,
                avg_fill_price, fee_asset, fee_amount, trade_ids_json, created_at_utc, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_order_id) DO UPDATE SET
                status=excluded.status,
                filled_qty=excluded.filled_qty,
                cumulative_quote_qty=excluded.cumulative_quote_qty,
                avg_fill_price=excluded.avg_fill_price,
                fee_asset=excluded.fee_asset,
                fee_amount=excluded.fee_amount,
                trade_ids_json=excluded.trade_ids_json,
                updated_at_utc=excluded.updated_at_utc
            """,
            (
                snap.client_order_id,
                snap.exchange_order_id or 0,
                snap.symbol,
                snap.side.value,
                snap.order_type,
                snap.status.value,
                str(snap.price),
                str(snap.stop_price) if snap.stop_price is not None else None,
                str(snap.orig_qty),
                str(snap.filled_qty),
                str(snap.cumulative_quote_qty),
                str(snap.avg_fill_price),
                snap.fee_asset,
                str(snap.fee_amount),
                json.dumps(list(snap.trade_ids)),
                snap.created_at_utc.isoformat(),
                snap.updated_at_utc.isoformat(),
            ),
        )
        self._persist_balances()

    @property
    def free_usdt(self) -> Decimal:
        return self._free_usdt

    @property
    def locked_usdt(self) -> Decimal:
        return self._locked_usdt

    @property
    def free_sui(self) -> Decimal:
        return self._free_sui

    @property
    def locked_sui(self) -> Decimal:
        return self._locked_sui

    @property
    def total_sui(self) -> Decimal:
        return self._free_sui + self._locked_sui

    @property
    def live_orders_sent_count(self) -> int:
        """Strictly 0 in P4 (INV-R1)."""
        return self._live_orders_sent_count

    def total_equity_usdt(self, mark_price_sui: Decimal) -> Decimal:
        mp = require_decimal(mark_price_sui, "mark_price_sui")
        return self._free_usdt + self._locked_usdt + (self.total_sui * mp)

    def inject_next_submit_timeout(
        self,
        mode: str,
        partial_fill_ratio: Decimal | None = None,
    ) -> None:
        """Configure deterministic fault injection for the next `submit_limit_buy` call (FI-P4-05).

        Supported modes:
        - `"TIMEOUT_BEFORE_ENGINE_ACCEPT"`: Raises `ExchangeTimeoutError` without creating order.
        - `"TIMEOUT_AFTER_ENGINE_ACCEPT_NEW"`: Creates `NEW` order, then raises `ExchangeTimeoutError`.
        - `"TIMEOUT_AFTER_ENGINE_ACCEPT_AND_FILL"`: Creates and fills order, then raises `ExchangeTimeoutError`.
        - `"TIMEOUT_AFTER_ENGINE_ACCEPT_PARTIAL"`: Creates and partially fills order, then raises `ExchangeTimeoutError`.
        """
        valid_modes = {
            "TIMEOUT_BEFORE_ENGINE_ACCEPT",
            "TIMEOUT_AFTER_ENGINE_ACCEPT_NEW",
            "TIMEOUT_AFTER_ENGINE_ACCEPT_AND_FILL",
            "TIMEOUT_AFTER_ENGINE_ACCEPT_PARTIAL",
        }
        if mode not in valid_modes:
            raise ValueError(f"Unsupported timeout injection mode: {mode}")
        self._injected_submit_timeout_mode = mode
        self._injected_submit_partial_ratio = partial_fill_ratio

    def submit_limit_buy(
        self,
        intent: OrderIntent,
        *,
        immediate_fill_qty: Decimal | None = None,
        immediate_fill_price: Decimal | None = None,
        now_utc: datetime | None = None,
    ) -> OrderSnapshot:
        """Submit an idempotent PAPER limit buy order.

        If `intent.client_order_id` already exists in the engine, returns the existing snapshot
        without creating a duplicate order (INV-R6).
        """
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        if intent.symbol != TRADING_SYMBOL:
            raise ValueError(f"INV-R2 violated: PaperOrderbookSimulator only accepts {TRADING_SYMBOL}")
        if intent.side != OrderSide.BUY:
            raise ValueError("submit_limit_buy only accepts OrderSide.BUY")
        if not intent.client_order_id.startswith("sui_p4_paper_"):
            raise ValueError(
                f"INV-R1 violated: clientOrderId '{intent.client_order_id}' must start with 'sui_p4_paper_'"
            )
        self.filters.validate_order(price=intent.price, qty=intent.qty)

        # Idempotent check: if client_order_id was already accepted, return existing state
        existing = self._orders.get(intent.client_order_id)
        if existing is not None:
            return existing

        timeout_mode = self._injected_submit_timeout_mode
        partial_ratio = self._injected_submit_partial_ratio
        self._injected_submit_timeout_mode = None
        self._injected_submit_partial_ratio = None

        if timeout_mode == "TIMEOUT_BEFORE_ENGINE_ACCEPT":
            raise ExchangeTimeoutError(
                f"Simulated network timeout BEFORE order {intent.client_order_id} reached matching engine"
            )

        required_usdt = intent.price * intent.qty * (
            Decimal("1") + (self.buy_fee_rate if self.commission_asset == "USDT" else Decimal("0"))
        )
        if self._free_usdt < required_usdt:
            raise ValueError(
                f"Insufficient free USDT ({self._free_usdt}) for order notional+fee ({required_usdt})"
            )

        self._free_usdt -= required_usdt
        self._locked_usdt += required_usdt

        oid = self._next_order_id
        self._next_order_id += 1

        snap = OrderSnapshot(
            client_order_id=intent.client_order_id,
            exchange_order_id=oid,
            symbol=intent.symbol,
            side=OrderSide.BUY,
            order_type="LIMIT",
            status=OrderStatus.NEW,
            price=intent.price,
            stop_price=None,
            orig_qty=intent.qty,
            filled_qty=Decimal("0.0"),
            cumulative_quote_qty=Decimal("0.0000"),
            avg_fill_price=Decimal("0.0000"),
            fee_asset=self.commission_asset,
            fee_amount=Decimal("0.0000"),
            trade_ids=(),
            created_at_utc=ref_now,
            updated_at_utc=ref_now,
        )
        self._persist_order(snap)

        if timeout_mode == "TIMEOUT_AFTER_ENGINE_ACCEPT_NEW":
            raise ExchangeTimeoutError(
                f"Simulated network timeout AFTER order {intent.client_order_id} was accepted as NEW"
            )

        if timeout_mode == "TIMEOUT_AFTER_ENGINE_ACCEPT_AND_FILL":
            self.simulate_buy_fill(
                client_order_id=intent.client_order_id,
                fill_qty=intent.qty,
                fill_price=immediate_fill_price or intent.price,
                now_utc=ref_now,
            )
            raise ExchangeTimeoutError(
                f"Simulated network timeout AFTER order {intent.client_order_id} was accepted and FILLED"
            )

        if timeout_mode == "TIMEOUT_AFTER_ENGINE_ACCEPT_PARTIAL":
            ratio = partial_ratio if partial_ratio is not None else Decimal("0.5")
            part_qty = self.filters.quantize_qty_down(intent.qty * ratio)
            if part_qty > Decimal("0"):
                self.simulate_buy_fill(
                    client_order_id=intent.client_order_id,
                    fill_qty=part_qty,
                    fill_price=immediate_fill_price or intent.price,
                    now_utc=ref_now,
                )
            raise ExchangeTimeoutError(
                f"Simulated network timeout AFTER order {intent.client_order_id} was PARTIALLY_FILLED"
            )

        if immediate_fill_qty is not None and immediate_fill_qty > Decimal("0"):
            snap = self.simulate_buy_fill(
                client_order_id=intent.client_order_id,
                fill_qty=immediate_fill_qty,
                fill_price=immediate_fill_price or intent.price,
                now_utc=ref_now,
            )

        return snap

    def simulate_buy_fill(
        self,
        *,
        client_order_id: str,
        fill_qty: Decimal,
        fill_price: Decimal | None = None,
        now_utc: datetime | None = None,
    ) -> OrderSnapshot:
        """Execute a full or partial fill on an active `NEW` or `PARTIALLY_FILLED` limit buy order."""
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        snap = self._orders.get(client_order_id)
        if snap is None:
            raise KeyError(f"Order '{client_order_id}' not found")
        if snap.side != OrderSide.BUY:
            raise ValueError("simulate_buy_fill only applies to BUY orders")
        if snap.status not in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}:
            return snap

        fq = require_decimal(fill_qty, "fill_qty")
        fp = require_decimal(fill_price if fill_price is not None else snap.price, "fill_price")
        if fq <= Decimal("0") or fq > snap.remaining_qty:
            raise ValueError(f"Invalid fill_qty {fq} for remaining_qty {snap.remaining_qty}")
        if fp > snap.price:
            raise ValueError(f"Limit BUY fill price {fp} cannot exceed limit price {snap.price}")

        quote_spent = fq * fp
        reserved_for_slice = fq * snap.price * (
            Decimal("1") + (self.buy_fee_rate if self.commission_asset == "USDT" else Decimal("0"))
        )
        self._locked_usdt = max(Decimal("0.0000"), self._locked_usdt - reserved_for_slice)

        if self.commission_asset == "SUI":
            fee_inc = (fq * self.buy_fee_rate).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
            net_sui_added = fq - fee_inc
            actual_usdt_cost = quote_spent
        elif self.commission_asset == "USDT":
            fee_inc = (quote_spent * self.buy_fee_rate).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
            net_sui_added = fq
            actual_usdt_cost = quote_spent + fee_inc
        else:  # BNB
            fee_inc = (quote_spent * self.buy_fee_rate * Decimal("0.75")).quantize(
                Decimal("0.000001"), rounding=ROUND_DOWN
            )
            net_sui_added = fq
            actual_usdt_cost = quote_spent
            self._free_bnb = max(Decimal("0"), self._free_bnb - Decimal("0.0001"))

        # Refund any price improvement savings back to free_usdt
        price_improvement_refund = max(Decimal("0.0000"), reserved_for_slice - actual_usdt_cost)
        self._free_usdt += price_improvement_refund
        self._free_sui += net_sui_added

        new_filled_qty = snap.filled_qty + fq
        new_cum_quote = snap.cumulative_quote_qty + quote_spent
        new_avg_price = (new_cum_quote / new_filled_qty).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
        new_fee = snap.fee_amount + fee_inc
        tid = self._next_trade_id
        self._next_trade_id += 1

        new_status = (
            OrderStatus.FILLED if new_filled_qty >= snap.orig_qty else OrderStatus.PARTIALLY_FILLED
        )
        updated = OrderSnapshot(
            client_order_id=snap.client_order_id,
            exchange_order_id=snap.exchange_order_id,
            symbol=snap.symbol,
            side=snap.side,
            order_type=snap.order_type,
            status=new_status,
            price=snap.price,
            stop_price=snap.stop_price,
            orig_qty=snap.orig_qty,
            filled_qty=new_filled_qty,
            cumulative_quote_qty=new_cum_quote,
            avg_fill_price=new_avg_price,
            fee_asset=snap.fee_asset,
            fee_amount=new_fee,
            trade_ids=snap.trade_ids + (tid,),
            created_at_utc=snap.created_at_utc,
            updated_at_utc=ref_now,
        )
        self._persist_order(updated)
        return updated

    def step_orderbook(
        self,
        orderbook: PartialOrderbookSnapshot,
        *,
        now_utc: datetime | None = None,
    ) -> list[OrderSnapshot]:
        """Match any open `NEW` / `PARTIALLY_FILLED` orders against an incoming 10-level orderbook snapshot."""
        ref_now = ensure_utc(now_utc or orderbook.recv_time_utc, "now_utc")
        updated_snapshots: list[OrderSnapshot] = []
        if not orderbook.bids or not orderbook.asks:
            return updated_snapshots

        best_ask_price, best_ask_qty = orderbook.asks[0]
        best_bid_price, _ = orderbook.bids[0]

        for cid, snap in list(self._orders.items()):
            if snap.status not in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}:
                continue
            if snap.side == OrderSide.BUY and snap.order_type == "LIMIT":
                if best_ask_price <= snap.price:
                    fill_q = self.filters.quantize_qty_down(min(snap.remaining_qty, best_ask_qty))
                    if fill_q >= self.filters.min_qty:
                        new_snap = self.simulate_buy_fill(
                            client_order_id=cid,
                            fill_qty=fill_q,
                            fill_price=min(snap.price, best_ask_price),
                            now_utc=ref_now,
                        )
                        updated_snapshots.append(new_snap)
            elif snap.side == OrderSide.SELL and snap.order_type == "STOP_LOSS_LIMIT":
                if snap.stop_price is not None and best_bid_price <= snap.stop_price:
                    new_snap = self._execute_sell_order_fill(
                        snap=snap,
                        exec_price=min(snap.price, best_bid_price),
                        now_utc=ref_now,
                    )
                    updated_snapshots.append(new_snap)

        return updated_snapshots

    def query_order_by_client_order_id(
        self,
        *,
        symbol: str,
        client_order_id: str,
        now_utc: datetime | None = None,
    ) -> OrderSnapshot:
        """Idempotent order lookup by `clientOrderId` for `ORDER_UNKNOWN` and `SYNCING` reconciliation (§3.4)."""
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        if symbol != TRADING_SYMBOL:
            raise ValueError(f"Unsupported symbol '{symbol}'")
        snap = self._orders.get(client_order_id)
        if snap is not None:
            return snap
        return OrderSnapshot(
            client_order_id=client_order_id,
            exchange_order_id=None,
            symbol=symbol,
            side=OrderSide.BUY,
            order_type="LIMIT",
            status=OrderStatus.NOT_FOUND,
            price=Decimal("0.0000"),
            stop_price=None,
            orig_qty=Decimal("0.0"),
            filled_qty=Decimal("0.0"),
            cumulative_quote_qty=Decimal("0.0000"),
            avg_fill_price=Decimal("0.0000"),
            fee_asset=self.commission_asset,
            fee_amount=Decimal("0.0000"),
            trade_ids=(),
            created_at_utc=ref_now,
            updated_at_utc=ref_now,
        )

    def cancel_order(
        self,
        *,
        symbol: str,
        client_order_id: str,
        now_utc: datetime | None = None,
    ) -> OrderSnapshot:
        """Idempotently cancel remaining quantity on an open BUY or SELL order.

        If the order was partially filled prior to cancellation, the executed `filled_qty` and
        `avg_fill_price` are strictly preserved on the returned snapshot while remaining locked funds
        are released.
        """
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        if symbol != TRADING_SYMBOL:
            raise ValueError(f"Unsupported symbol '{symbol}'")
        snap = self._orders.get(client_order_id)
        if snap is None:
            return self.query_order_by_client_order_id(
                symbol=symbol, client_order_id=client_order_id, now_utc=ref_now
            )
        if snap.status in {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}:
            return snap

        rem_qty = snap.remaining_qty
        if snap.side == OrderSide.BUY:
            unspent_locked = rem_qty * snap.price * (
                Decimal("1") + (self.buy_fee_rate if self.commission_asset == "USDT" else Decimal("0"))
            )
            self._locked_usdt = max(Decimal("0.0000"), self._locked_usdt - unspent_locked)
            self._free_usdt += unspent_locked
        else:
            self._locked_sui = max(Decimal("0.0"), self._locked_sui - rem_qty)
            self._free_sui += rem_qty

        canceled = OrderSnapshot(
            client_order_id=snap.client_order_id,
            exchange_order_id=snap.exchange_order_id,
            symbol=snap.symbol,
            side=snap.side,
            order_type=snap.order_type,
            status=OrderStatus.CANCELED,
            price=snap.price,
            stop_price=snap.stop_price,
            orig_qty=snap.orig_qty,
            filled_qty=snap.filled_qty,
            cumulative_quote_qty=snap.cumulative_quote_qty,
            avg_fill_price=snap.avg_fill_price,
            fee_asset=snap.fee_asset,
            fee_amount=snap.fee_amount,
            trade_ids=snap.trade_ids,
            created_at_utc=snap.created_at_utc,
            updated_at_utc=ref_now,
        )
        self._persist_order(canceled)
        return canceled

    def submit_stop_loss_protection_order(
        self,
        *,
        symbol: str,
        client_order_id: str,
        qty: Decimal,
        stop_trigger_price: Decimal,
        limit_price: Decimal | None = None,
        now_utc: datetime | None = None,
    ) -> OrderSnapshot:
        """Place a -1% protective stop-loss order locking `qty` SUI in `locked_sui` (§3.5, INV-R7)."""
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        if symbol != TRADING_SYMBOL:
            raise ValueError(f"INV-R2 violated: symbol must be {TRADING_SYMBOL}")
        if not client_order_id.startswith("sui_p4_paper_"):
            raise ValueError("INV-R1 violated: protection clientOrderId must start with 'sui_p4_paper_'")

        existing = self._orders.get(client_order_id)
        if existing is not None and existing.status != OrderStatus.CANCELED:
            return existing

        q = require_decimal(qty, "qty")
        sp = require_decimal(stop_trigger_price, "stop_trigger_price")
        lp = require_decimal(limit_price if limit_price is not None else sp, "limit_price")
        self.filters.validate_order(price=lp, qty=q)

        if q > self._free_sui:
            raise ValueError(
                f"Cannot lock {q} SUI for protection order: only {self._free_sui} free SUI available"
            )

        self._free_sui -= q
        self._locked_sui += q
        oid = self._next_order_id
        self._next_order_id += 1

        snap = OrderSnapshot(
            client_order_id=client_order_id,
            exchange_order_id=oid,
            symbol=symbol,
            side=OrderSide.SELL,
            order_type="STOP_LOSS_LIMIT",
            status=OrderStatus.NEW,
            price=lp,
            stop_price=sp,
            orig_qty=q,
            filled_qty=Decimal("0.0"),
            cumulative_quote_qty=Decimal("0.0000"),
            avg_fill_price=Decimal("0.0000"),
            fee_asset="USDT",
            fee_amount=Decimal("0.0000"),
            trade_ids=(),
            created_at_utc=ref_now,
            updated_at_utc=ref_now,
        )
        self._persist_order(snap)
        return snap

    def _execute_sell_order_fill(
        self,
        *,
        snap: OrderSnapshot,
        exec_price: Decimal,
        now_utc: datetime,
    ) -> OrderSnapshot:
        q = snap.remaining_qty
        self._locked_sui = max(Decimal("0.0"), self._locked_sui - q)
        gross_usdt = q * exec_price
        fee_usdt = (gross_usdt * self.sell_fee_rate).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        net_usdt = gross_usdt - fee_usdt
        self._free_usdt += net_usdt
        tid = self._next_trade_id
        self._next_trade_id += 1

        filled = OrderSnapshot(
            client_order_id=snap.client_order_id,
            exchange_order_id=snap.exchange_order_id,
            symbol=snap.symbol,
            side=OrderSide.SELL,
            order_type=snap.order_type,
            status=OrderStatus.FILLED,
            price=snap.price,
            stop_price=snap.stop_price,
            orig_qty=snap.orig_qty,
            filled_qty=snap.orig_qty,
            cumulative_quote_qty=gross_usdt,
            avg_fill_price=exec_price,
            fee_asset="USDT",
            fee_amount=fee_usdt,
            trade_ids=snap.trade_ids + (tid,),
            created_at_utc=snap.created_at_utc,
            updated_at_utc=now_utc,
        )
        self._persist_order(filled)
        return filled

    def submit_exit_sell_order(
        self,
        *,
        symbol: str,
        client_order_id: str,
        qty: Decimal,
        exec_price: Decimal,
        cancel_protection_client_order_id: str | None = None,
        now_utc: datetime | None = None,
    ) -> OrderSnapshot:
        """Atomically release any active protection order and execute an exit SELL order (§3.2)."""
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        if symbol != TRADING_SYMBOL:
            raise ValueError(f"INV-R2 violated: symbol must be {TRADING_SYMBOL}")
        if not client_order_id.startswith("sui_p4_paper_"):
            raise ValueError("INV-R1 violated: exit clientOrderId must start with 'sui_p4_paper_'")

        existing = self._orders.get(client_order_id)
        if existing is not None and existing.status == OrderStatus.FILLED:
            return existing

        if cancel_protection_client_order_id is not None:
            self.cancel_order(
                symbol=symbol,
                client_order_id=cancel_protection_client_order_id,
                now_utc=ref_now,
            )

        q = require_decimal(qty, "qty")
        ep = self.filters.quantize_price_down(require_decimal(exec_price, "exec_price"))
        self.filters.validate_order(price=ep, qty=q)

        if q > self._free_sui:
            raise ValueError(f"Insufficient free SUI ({self._free_sui}) to exit {q} SUI")

        self._free_sui -= q
        self._locked_sui += q
        oid = self._next_order_id
        self._next_order_id += 1

        new_sell = OrderSnapshot(
            client_order_id=client_order_id,
            exchange_order_id=oid,
            symbol=symbol,
            side=OrderSide.SELL,
            order_type="LIMIT",
            status=OrderStatus.NEW,
            price=ep,
            stop_price=None,
            orig_qty=q,
            filled_qty=Decimal("0.0"),
            cumulative_quote_qty=Decimal("0.0000"),
            avg_fill_price=Decimal("0.0000"),
            fee_asset="USDT",
            fee_amount=Decimal("0.0000"),
            trade_ids=(),
            created_at_utc=ref_now,
            updated_at_utc=ref_now,
        )
        self._persist_order(new_sell)
        return self._execute_sell_order_fill(snap=new_sell, exec_price=ep, now_utc=ref_now)

    def list_open_orders(self, side: OrderSide | None = None) -> list[OrderSnapshot]:
        res = [
            s
            for s in self._orders.values()
            if s.status in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}
        ]
        if side is not None:
            res = [s for s in res if s.side == side]
        return res

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


# Alias matching P4_PLAN §3.7
SimulatedExchange = PaperOrderbookSimulator
