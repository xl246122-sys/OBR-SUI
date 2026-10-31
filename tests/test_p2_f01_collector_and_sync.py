"""P2 Automated Verification Suite: F-01 Market Collector, Quality Guard, 23h Hot Rotation,
100ms Orderbook Backpressure, and E-01 Cloud-to-Local Incremental Parquet/DuckDB Sync (QA-01, QA-02).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from sui_quant.contracts import DataQuality, MarketFeatures
from sui_quant.daemon.alert_manager import AlertManager
from sui_quant.daemon.storage import StateStore
from sui_quant.data.collector import F01MarketCollector
from sui_quant.data.quality_guard import (
    CALIBRATION_UNRECOVERABLE_TAG,
    PARTIAL_BOOK_SCOPE_DECLARATION,
    ClosedKlineBar,
)
from sui_quant.data.sync_engine import (
    CloudShardExporter,
    LocalResearchSyncEngine,
    SecretSyncBlockedError,
    SyncIntegrityError,
    assert_safe_for_sync,
)
from sui_quant.state_machine import TradingState


def _make_collector(tmp_path: Path, **kwargs) -> tuple[F01MarketCollector, StateStore, AlertManager]:
    store = StateStore(tmp_path / "state.db")
    store.set_trading_state(TradingState.READY_NO_POSITION)
    alert_mgr = AlertManager(store)
    collector = F01MarketCollector(
        store=store,
        alert_manager=alert_mgr,
        spool_dir=tmp_path / "spool",
        **kwargs,
    )
    return collector, store, alert_mgr


def test_qa01_duplicate_and_out_of_order_rejection(tmp_path: Path) -> None:
    """QA-01: Verify duplicate and out-of-order aggTrade, depth10, and bookTicker frames are rejected."""
    collector, store, _ = _make_collector(tmp_path)
    base_utc = datetime(2026, 10, 31, 6, 0, 0, tzinfo=timezone.utc)
    ex_ms = int(base_utc.timestamp() * 1000)

    # 1. Valid aggTrade #100
    msg_100 = {
        "stream": "suiusdt@aggTrade",
        "data": {"e": "aggTrade", "E": ex_ms, "s": "SUIUSDT", "a": 100, "p": "2.1500", "q": "50.0", "m": False},
    }
    assert collector.process_ws_message(msg_100, recv_time_utc=base_utc) == DataQuality.OK
    assert collector.trade_buffers["SUIUSDT"].size == 1

    # 2. Duplicate aggTrade #100 -> rejected as DUPLICATE
    assert collector.process_ws_message(msg_100, recv_time_utc=base_utc) == DataQuality.DUPLICATE
    assert collector.quality_guard.duplicate_count == 1
    assert collector.trade_buffers["SUIUSDT"].size == 1

    # 3. Valid aggTrade #102
    msg_102 = {
        "stream": "suiusdt@aggTrade",
        "data": {"e": "aggTrade", "E": ex_ms + 50, "s": "SUIUSDT", "a": 102, "p": "2.1510", "q": "30.0", "m": True},
    }
    assert collector.process_ws_message(msg_102, recv_time_utc=base_utc) == DataQuality.OK
    assert collector.trade_buffers["SUIUSDT"].size == 2

    # 4. Out-of-order aggTrade #101 (arriving after #102) -> rejected as OUT_OF_ORDER
    msg_101_late = {
        "stream": "suiusdt@aggTrade",
        "data": {"e": "aggTrade", "E": ex_ms + 20, "s": "SUIUSDT", "a": 101, "p": "2.1490", "q": "10.0", "m": False},
    }
    assert collector.process_ws_message(msg_101_late, recv_time_utc=base_utc) == DataQuality.OUT_OF_ORDER
    assert collector.quality_guard.out_of_order_count == 1
    assert collector.trade_buffers["SUIUSDT"].latest().agg_trade_id == 102

    # 5. Depth10 duplicate & out-of-order check
    depth_500 = {
        "stream": "suiusdt@depth10@100ms",
        "data": {
            "lastUpdateId": 500,
            "E": ex_ms,
            "bids": [["2.1500", "1000.0"], ["2.1499", "500.0"]],
            "asks": [["2.1502", "800.0"], ["2.1503", "600.0"]],
        },
    }
    depth_499_old = {
        "stream": "suiusdt@depth10@100ms",
        "data": {
            "lastUpdateId": 499,
            "E": ex_ms,
            "bids": [["2.1400", "100.0"]],
            "asks": [["2.1600", "100.0"]],
        },
    }
    assert collector.process_ws_message(depth_500, recv_time_utc=base_utc) == DataQuality.OK
    assert collector.process_ws_message(depth_500, recv_time_utc=base_utc) == DataQuality.DUPLICATE
    assert collector.process_ws_message(depth_499_old, recv_time_utc=base_utc) == DataQuality.OUT_OF_ORDER
    snap = collector.depth_buffers["SUIUSDT"].latest()
    assert snap is not None
    assert snap.last_update_id == 500
    assert snap.scope == PARTIAL_BOOK_SCOPE_DECLARATION
    store.close()


def test_qa01_kline_close_once_and_unclosed_isolation(tmp_path: Path) -> None:
    """QA-01: Verify unclosed K-lines (x=False) never trigger 5m signal, and duplicate closed K-lines (x=True)
    trigger the 5m close signal strictly ONCE.
    """
    emitted_5m: list[ tuple[ClosedKlineBar, MarketFeatures] ] = []
    collector, store, _ = _make_collector(
        tmp_path,
        on_5m_bar_closed=lambda bar, feat: emitted_5m.append((bar, feat)),
    )
    base_utc = datetime(2026, 10, 31, 6, 5, 0, tzinfo=timezone.utc)
    ex_ms = int(base_utc.timestamp() * 1000)
    bar_open_ms = ex_ms - 300_000
    bar_close_ms = ex_ms - 1

    # 1. Inject 10 unclosed (x=False) updates for the same 5m bar
    for i in range(10):
        frame_utc = base_utc - timedelta(seconds=10 - i)
        frame_ms = int(frame_utc.timestamp() * 1000)
        unclosed_msg = {
            "stream": "suiusdt@kline_5m",
            "data": {
                "e": "kline",
                "E": frame_ms,
                "s": "SUIUSDT",
                "k": {
                    "t": bar_open_ms,
                    "T": bar_close_ms,
                    "s": "SUIUSDT",
                    "i": "5m",
                    "o": "2.1000",
                    "h": f"2.1{i}00",
                    "l": "2.0900",
                    "c": f"2.1{i}00",
                    "v": f"{1000 + i * 100}.0",
                    "q": f"{2100 + i * 210}.0",
                    "n": 50 + i,
                    "x": False,
                },
            },
        }
        q = collector.process_ws_message(unclosed_msg, recv_time_utc=frame_utc)
        assert q == DataQuality.OK

    # Unclosed updates must NEVER appear in closed bars or trigger the 5m callback
    assert len(emitted_5m) == 0
    assert collector.five_min_signal_trigger_count == 0
    assert len(collector.quality_guard.get_closed_bars("SUIUSDT", "5m")) == 0
    transient = collector.quality_guard.get_unclosed_transient_view("SUIUSDT", "5m")
    assert transient is not None
    assert transient.is_closed is False
    assert transient.current_price == Decimal("2.1900")

    # 2. Inject the closed (x=True) update 3 times (simulating duplicate WS push + reconnect replay)
    closed_msg = {
        "stream": "suiusdt@kline_5m",
        "data": {
            "e": "kline",
            "E": ex_ms,
            "s": "SUIUSDT",
            "k": {
                "t": bar_open_ms,
                "T": bar_close_ms,
                "s": "SUIUSDT",
                "i": "5m",
                "o": "2.1000",
                "h": "2.2000",
                "l": "2.0900",
                "c": "2.1950",
                "v": "2500.0",
                "q": "5400.0",
                "V": "1500.0",
                "Q": "3240.0",
                "n": 120,
                "x": True,
            },
        },
    }
    assert collector.process_ws_message(closed_msg, recv_time_utc=base_utc) == DataQuality.OK
    assert collector.process_ws_message(closed_msg, recv_time_utc=base_utc) == DataQuality.DUPLICATE
    assert collector.process_ws_message(closed_msg, recv_time_utc=base_utc) == DataQuality.DUPLICATE

    # Exactly 1 emission!
    assert len(emitted_5m) == 1
    assert collector.five_min_signal_trigger_count == 1
    closed_bar, features = emitted_5m[0]
    assert closed_bar.is_closed is True
    assert closed_bar.close_price == Decimal("2.1950")
    assert features.complete_bar_only is True
    assert features.features["close"] == Decimal("2.1950")
    assert collector.quality_guard.get_unclosed_transient_view("SUIUSDT", "5m") is None
    store.close()


def test_qa01_ws_disconnect_gap_marking_and_kline_backfill(tmp_path: Path) -> None:
    """QA-01 & §14.1: Verify 3-minute WS disconnect immediately freezes new entries, backfills closed K-lines via REST,
    and permanently marks the depth10 orderbook gap as GAP_UNRECOVERABLE_FOR_FILL_CALIBRATION.
    """
    collector, store, _ = _make_collector(tmp_path)
    t0 = datetime(2026, 10, 31, 6, 0, 0, tzinfo=timezone.utc)
    ex_ms = int(t0.timestamp() * 1000)

    # Seed pre-disconnect depth10 snapshot (lastUpdateId=900)
    collector.process_ws_message(
        {
            "stream": "suiusdt@depth10@100ms",
            "data": {"lastUpdateId": 900, "E": ex_ms, "bids": [["2.1000", "500.0"]], "asks": [["2.1005", "500.0"]]},
        },
        recv_time_utc=t0,
    )
    assert store.get_trading_state() == TradingState.READY_NO_POSITION

    # Inject unexpected 3-minute disconnect
    t_disc = t0 + timedelta(seconds=10)
    collector.handle_unexpected_disconnect(now_utc=t_disc, reason="NETWORK_DROP_3MIN")
    assert collector.quality_guard.current_quality == DataQuality.GAP_DETECTED
    assert store.get_trading_state() == TradingState.ENTRY_FROZEN

    # Reconnect 3 minutes later with REST backfilled 1m closed K-lines
    t_recon = t_disc + timedelta(minutes=3)
    backfilled_1m = [
        {
            "t": int((t_disc + timedelta(minutes=m)).timestamp() * 1000),
            "T": int((t_disc + timedelta(minutes=m + 1) - timedelta(milliseconds=1)).timestamp() * 1000),
            "s": "SUIUSDT",
            "i": "1m",
            "o": "2.1000",
            "h": "2.1100",
            "l": "2.0950",
            "c": "2.1050",
            "v": "800.0",
            "q": "1680.0",
            "n": 40,
            "x": True,
        }
        for m in range(3)
    ]
    marker = collector.recover_from_disconnect(
        reconnected_at_utc=t_recon,
        backfilled_klines_by_symbol={("SUIUSDT", "1m"): backfilled_1m},
        first_depth_update_id_after_reconnect=1450,
    )

    assert collector.quality_guard.current_quality == DataQuality.OK
    assert len(collector.quality_guard.get_closed_bars("SUIUSDT", "1m")) == 3
    assert all(b.backfilled_via_rest for b in collector.quality_guard.get_closed_bars("SUIUSDT", "1m"))

    # Verify unrecoverable orderbook gap marker properties
    assert marker.recoverable is False
    assert marker.calibration_tag == CALIBRATION_UNRECOVERABLE_TAG
    assert marker.allow_b01_fill_calibration is False
    assert marker.last_update_id_before_gap == 900
    assert marker.first_update_id_after_gap == 1450

    # Window overlapping the 3-minute disconnect is forbidden for B-01 fill calibration
    assert (
        collector.quality_guard.is_window_valid_for_b01_calibration(
            "SUIUSDT",
            t_disc + timedelta(seconds=30),
            t_disc + timedelta(seconds=90),
        )
        is False
    )
    # Clean window after reconnect is allowed
    assert (
        collector.quality_guard.is_window_valid_for_b01_calibration(
            "SUIUSDT",
            t_recon + timedelta(seconds=10),
            t_recon + timedelta(seconds=60),
        )
        is True
    )
    store.close()


def test_qa01_23h_hot_connection_rotation_zero_gap(tmp_path: Path) -> None:
    """QA-01: Verify proactive 23-hour dual-connection hot rotation achieves zero gaps and zero duplicates."""
    collector, store, _ = _make_collector(tmp_path)
    t_start = datetime(2026, 10, 31, 0, 0, 0, tzinfo=timezone.utc)
    collector.active_conn_opened_utc = t_start

    # At 22h 59m -> no rotation yet
    assert collector.should_rotate_connection_23h(now_utc=t_start + timedelta(hours=22, minutes=59)) is False
    # At 23h 00m 01s -> rotation triggered
    t_rot = t_start + timedelta(hours=23, seconds=1)
    assert collector.should_rotate_connection_23h(now_utc=t_rot) is True

    ex_ms = int(t_rot.timestamp() * 1000)

    def _agg_frame(seq_id: int) -> tuple[dict, datetime, int]:
        return (
            {
                "stream": "suiusdt@aggTrade",
                "data": {
                    "e": "aggTrade",
                    "E": ex_ms + seq_id,
                    "s": "SUIUSDT",
                    "a": seq_id,
                    "p": "2.2000",
                    "q": "15.0",
                    "m": False,
                },
            },
            t_rot,
            1_000_000_000 + seq_id * 1_000,
        )

    # Primary receives 201..205; Standby receives 203..208 (overlapping on 203, 204, 205)
    primary_frames = [_agg_frame(i) for i in range(201, 206)]
    standby_frames = [_agg_frame(i) for i in range(203, 209)]

    report = collector.execute_23h_hot_rotation(
        new_conn_id="WS-ROTATED-002",
        overlapping_primary_frames=primary_frames,
        overlapping_standby_frames=standby_frames,
        rotated_at_utc=t_rot,
    )

    assert report["accepted_unique_events"] == 8  # 201, 202, 203, 204, 205, 206, 207, 208
    assert report["deduplicated_overlap_events"] == 3  # 203, 204, 205
    assert collector.active_conn_id == "WS-ROTATED-002"
    assert collector.standby_conn_id is None
    assert collector.hot_rotation_count == 1
    recorded_ids = [t.agg_trade_id for t in collector.trade_buffers["SUIUSDT"].items()]
    assert recorded_ids == [201, 202, 203, 204, 205, 206, 207, 208]
    store.close()


def test_qa02_100ms_depth_backpressure_and_bounded_memory(tmp_path: Path) -> None:
    """QA-02: Verify 100ms depth burst respects bounded ring buffer & queue limits, triggers automatic
    1000ms degradation at 80% watermark, preserves closed bars on overflow, freezes on stale data,
    and meets P99 <= 50ms intra-process latency SLO.
    """
    collector, store, _ = _make_collector(
        tmp_path,
        queue_maxsize=100,
        depth_ring_capacity=50,
    )
    now_utc = datetime(2026, 10, 31, 6, 30, 0, tzinfo=timezone.utc)
    ex_ms = int(now_utc.timestamp() * 1000)

    # Enqueue 120 depth10 frames + 1 critical closed 5m kline into a maxsize=100 queue before draining
    for i in range(1, 121):
        collector.enqueue_raw_message(
            {
                "stream": "suiusdt@depth10@100ms",
                "data": {
                    "lastUpdateId": 1000 + i,
                    "E": ex_ms,
                    "bids": [["2.1500", "200.0"]],
                    "asks": [["2.1505", "200.0"]],
                },
            },
            recv_time_utc=now_utc,
        )
        if i == 90:
            # High watermark (80/100) reached -> automatically degraded to 1000ms
            assert collector.depth_sampling_ms == 1000
            assert collector.ingestion_queue.degraded_mode is True

    # Enqueue a critical closed 5m K-line while queue is full (qsize == 100)
    collector.enqueue_raw_message(
        {
            "stream": "suiusdt@kline_5m",
            "data": {
                "e": "kline",
                "E": ex_ms,
                "s": "SUIUSDT",
                "k": {
                    "t": ex_ms - 300_000,
                    "T": ex_ms - 1,
                    "s": "SUIUSDT",
                    "i": "5m",
                    "o": "2.1500",
                    "h": "2.1800",
                    "l": "2.1400",
                    "c": "2.1750",
                    "v": "5000.0",
                    "q": "10800.0",
                    "n": 88,
                    "x": True,
                },
            },
        },
        recv_time_utc=now_utc,
    )

    # Queue strictly bounded at maxsize=100
    assert collector.ingestion_queue.qsize == 100
    assert collector.ingestion_queue.peak_size == 100
    assert collector.ingestion_queue.dropped_count == 21  # 21 non-critical depth frames shed, closed bar kept!

    # Drain all items
    drained = collector.drain_and_process_queue(max_items=200)
    assert drained == 100
    # Critical 5m closed bar survived the overflow and was processed!
    assert collector.five_min_signal_trigger_count == 1
    # RingBuffer capacity strictly bounded at 50
    assert collector.depth_buffers["SUIUSDT"].size == 50
    assert collector.depth_buffers["SUIUSDT"].total_evicted > 0

    # Verify intra-process latency P99 <= 50ms SLO
    lat = collector.compute_latency_percentiles()
    assert lat.sample_count == 100
    assert lat.within_slo is True
    assert lat.p99_ms <= Decimal("50.0")

    # Inject stale market event (lag = 3.5s > 2.0s threshold) -> DataQuality.STALE + ENTRY_FROZEN
    stale_recv_utc = now_utc + timedelta(milliseconds=3500)
    q_stale = collector.process_ws_message(
        {
            "stream": "suiusdt@aggTrade",
            "data": {"e": "aggTrade", "E": ex_ms, "s": "SUIUSDT", "a": 9999, "p": "2.1500", "q": "10.0", "m": False},
        },
        recv_time_utc=stale_recv_utc,
    )
    assert q_stale == DataQuality.STALE
    assert collector.quality_guard.current_quality == DataQuality.STALE
    assert store.get_trading_state() == TradingState.ENTRY_FROZEN
    store.close()


def test_p2_dual_tier_1s_fast_scan_and_btc_crash_guard(tmp_path: Path) -> None:
    """Verify 1s fast scan computes microstructure metrics, never directly triggers a buy,
    and freezes new entries when BTCUSDT drops rapidly.
    """
    collector, store, _ = _make_collector(tmp_path)
    now_utc = datetime(2026, 10, 31, 6, 40, 0, tzinfo=timezone.utc)
    ex_ms = int(now_utc.timestamp() * 1000)

    collector.process_ws_message(
        {
            "stream": "suiusdt@bookTicker",
            "data": {"u": 10, "s": "SUIUSDT", "b": "2.0000", "B": "1000.0", "a": "2.0010", "A": "800.0", "E": ex_ms},
        },
        recv_time_utc=now_utc,
    )
    # Inject BTCUSDT rapid drop (-2.0% in 1s window)
    collector.process_ws_message(
        {
            "stream": "btcusdt@aggTrade",
            "data": {"e": "aggTrade", "E": ex_ms, "s": "BTCUSDT", "a": 1, "p": "70000.00", "q": "1.0", "m": False},
        },
        recv_time_utc=now_utc,
    )
    collector.process_ws_message(
        {
            "stream": "btcusdt@aggTrade",
            "data": {"e": "aggTrade", "E": ex_ms + 500, "s": "BTCUSDT", "a": 2, "p": "68600.00", "q": "2.5", "m": True},
        },
        recv_time_utc=now_utc,
    )

    scan = collector.run_1s_fast_scan(now_utc=now_utc)
    assert scan.can_directly_trigger_buy is False
    assert scan.btc_rapid_drop_warning is True
    assert scan.btc_short_return_pct == Decimal("-2.0")
    assert store.get_trading_state() == TradingState.ENTRY_FROZEN
    store.close()


def test_p2_incremental_sync_checkpoint_sha256_and_secret_block(tmp_path: Path) -> None:
    """Verify E-01 cloud shard export, Windows checkpoint resume, SHA-256 tamper rejection,
    idempotent Parquet + DuckDB ingestion, and secret file blocking.
    """
    cloud_spool = tmp_path / "cloud_spool"
    local_warehouse = tmp_path / "windows_warehouse"

    exporter = CloudShardExporter(spool_dir=cloud_spool)
    t_now = datetime(2026, 10, 31, 6, 50, 0, tzinfo=timezone.utc)

    # 1. Export 3 shards on cloud
    exporter.write_shard(
        "SUIUSDT",
        "kline_closed",
        [
            {
                "record_type": "kline_closed",
                "symbol": "SUIUSDT",
                "interval": "5m",
                "open_time_ms": 1700000000000 + i * 300000,
                "close_time_ms": 1700000299999 + i * 300000,
                "open_price": "2.1000",
                "high_price": "2.1500",
                "low_price": "2.0900",
                "close_price": "2.1400",
                "base_volume": "1200.0",
                "quote_volume": "2568.0",
                "exchange_time_utc": t_now.isoformat(),
                "recv_time_utc": t_now.isoformat(),
            }
            for i in range(2)
        ],
        created_at_utc=t_now,
    )
    exporter.write_shard(
        "SUIUSDT",
        "aggTrade",
        [
            {
                "record_type": "aggTrade",
                "symbol": "SUIUSDT",
                "agg_trade_id": 5001 + i,
                "price": "2.1450",
                "qty": "25.0",
                "is_buyer_maker": False,
                "exchange_time_utc": t_now.isoformat(),
                "recv_time_utc": t_now.isoformat(),
            }
            for i in range(3)
        ],
        created_at_utc=t_now,
    )
    s3 = exporter.write_shard(
        "BTCUSDT",
        "aggTrade",
        [
            {
                "record_type": "aggTrade",
                "symbol": "BTCUSDT",
                "agg_trade_id": 9001,
                "price": "69500.00",
                "qty": "0.5",
                "is_buyer_maker": True,
                "exchange_time_utc": t_now.isoformat(),
                "recv_time_utc": t_now.isoformat(),
            }
        ],
        created_at_utc=t_now,
    )

    manifest = exporter.get_manifest_dict()
    sync_engine = LocalResearchSyncEngine(local_warehouse_dir=local_warehouse)

    # 2. Simulate partial sync interrupted after 1 shard
    r1 = sync_engine.sync_from_manifest(
        manifest,
        fetch_shard_bytes_fn=lambda fname: (cloud_spool / fname).read_bytes(),
        max_shards_this_run=1,
    )
    assert r1.synced_shards_count == 1
    assert r1.last_synced_seq == 1
    assert r1.duckdb_total_rows == 2

    # 3. Resume sync -> skips shard 1, syncs shards 2 & 3 into Parquet + DuckDB
    r2 = sync_engine.sync_from_manifest(
        manifest,
        fetch_shard_bytes_fn=lambda fname: (cloud_spool / fname).read_bytes(),
    )
    assert r2.skipped_already_synced_count == 1
    assert r2.synced_shards_count == 2
    assert r2.last_synced_seq == 3
    assert r2.parquet_files_count == 3
    assert r2.duckdb_total_rows == 6  # 2 klines + 3 SUI trades + 1 BTC trade

    # 4. Re-running sync is 100% idempotent (0 new shards, 0 duplicate rows in DuckDB)
    r3 = sync_engine.sync_from_manifest(
        manifest,
        fetch_shard_bytes_fn=lambda fname: (cloud_spool / fname).read_bytes(),
    )
    assert r3.synced_shards_count == 0
    assert r3.skipped_already_synced_count == 3
    assert r3.duckdb_total_rows == 6

    # 5. Tamper with shard 3 bytes -> SHA-256 verification must raise SyncIntegrityError
    ckpt = sync_engine.load_checkpoint()
    ckpt["synced_shards"].pop(s3.shard_id)
    sync_engine._save_checkpoint(last_seq=2, synced_shards=ckpt["synced_shards"])

    with pytest.raises(SyncIntegrityError, match="SHA-256 mismatch"):
        sync_engine.sync_from_manifest(
            manifest,
            fetch_shard_bytes_fn=lambda fname: b"TAMPERED_PAYLOAD"
            if fname == s3.filename
            else (cloud_spool / fname).read_bytes(),
        )

    # 6. Verify strict secret & credential file blocking
    for forbidden_name in (
        ".env",
        ".env.production",
        "binance_api_key.pem",
        "ed25519_private.key",
        "ServerStation-account.txt",
        "consumed_live_tokens.db",
        "user_secret_config.json",
        "../outside.jsonl",
    ):
        with pytest.raises(SecretSyncBlockedError):
            assert_safe_for_sync(forbidden_name)

    with pytest.raises(SecretSyncBlockedError):
        exporter.write_shard(
            "SUIUSDT",
            "aggTrade",
            [{"symbol": "SUIUSDT", "api_secret": "SHOULD_NEVER_EXPORT"}],
        )
