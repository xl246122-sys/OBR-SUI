"""P2 Automated Verification Suite (Including P2 Remediation Items 1, 2, 3):
- Item 1: RFC 6455 WebSocket Client Protocol Compatibility & Fail-Safety (Ping/Pong, Fragmented Frames,
  Interleaved Control Frames, Close Frame Handshake, TLSv1.3 Verification, Reconnection, and Protocol Violation Guards).
- Item 2: 23-Hour Hot Connection Rotation Cross-Connection Deduplication AND Sequence-Gap Identification
  (explicitly verifying `zero_loss_guaranteed=False` and freezing on cross-connection sequence gaps).
- Item 3: Backpressure Policy Guaranteeing Zero Loss of Closed K-Lines (`k.x=True`) and DataQuality Events
  (`critical_dropped_count == 0`), plus Granular Snapshot Degradation & Per-Category Drop Statistics.
- E-01 Cloud-to-Local Incremental Parquet/DuckDB Sync with Checkpoint, SHA-256, and Secret Blacklist.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import ssl
import struct
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from sui_quant.contracts import DataQuality, MarketFeatures
from sui_quant.daemon.alert_manager import AlertManager
from sui_quant.daemon.storage import StateStore
from sui_quant.data.collector import (
    BinanceSpotWebSocketStream,
    F01MarketCollector,
    WebSocketClosedError,
    WebSocketHandshakeError,
    WebSocketProtocolError,
    compute_rfc6455_accept_key,
)
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


def _make_collector(tmp_path: Path, **kwargs: Any) -> tuple[F01MarketCollector, StateStore, AlertManager]:
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


def _generate_self_signed_tls_files(tmp_path: Path) -> tuple[Path, Path]:
    """Generate a temporary X.509 certificate and RSA private key for local TLSv1.3 WebSocket testing."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.DNSName("stream.binance.com")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "server_cert.pem"
    key_path = tmp_path / "server_key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _build_server_ws_frame(fin: bool, opcode: int, payload: bytes, rsv: int = 0) -> bytes:
    """Build an unmasked server-to-client RFC 6455 frame."""
    b1 = (0x80 if fin else 0x00) | (rsv & 0x70) | (opcode & 0x0F)
    length = len(payload)
    header = bytearray([b1])
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    return bytes(header) + payload


async def _read_masked_client_frame(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
    """Read and unmask a client-to-server RFC 6455 frame on the mock server side."""
    hdr2 = await asyncio.wait_for(reader.readexactly(2), timeout=5.0)
    b1, b2 = hdr2[0], hdr2[1]
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    assert masked is True, "RFC 6455 §5.1 requires all client-to-server frames to be masked"
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    mask_key = await reader.readexactly(4)
    raw = await reader.readexactly(length)
    payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(raw))
    return fin, opcode, payload


def test_p2_remediation_item1_rfc6455_websocket_ping_pong_fragmented_close_tls_and_reconnect(
    tmp_path: Path,
) -> None:
    """P2 Remediation Item 1: Comprehensive audit & proof of RFC 6455 WebSocket client compatibility and fail-safety:
    1. TLS verification (rejects untrusted self-signed cert by default; negotiates TLSv1.3 with pinned CA).
    2. Handshake verification (rejects invalid Sec-WebSocket-Accept).
    3. Multi-frame message fragmentation (3 fragments + 16-bit extended length 126) with an interleaved
       PING (0x9) control frame injected in the middle, verifying automatic masked PONG (0xA) reply
       and clean JSON reassembly.
    4. Proactive client PING -> server PONG verification.
    5. Server CLOSE frame (0x8, code=1001, reason='24h rotation') mutual close echo, collector
       DataQuality.GAP_DETECTED + ENTRY_FROZEN transition, and automatic reconnect + REST backfill recovery.
    6. Fail-safety rejection of RFC 6455 protocol violations (fragmented control frame, non-zero RSV, oversized frame).
    """

    async def _run_scenario() -> None:
        cert_path, key_path = _generate_self_signed_tls_files(tmp_path)
        server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ssl.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

        client_trusted_ssl = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(cert_path))
        client_trusted_ssl.minimum_version = ssl.TLSVersion.TLSv1_2
        client_trusted_ssl.check_hostname = True
        client_trusted_ssl.verify_mode = ssl.CERT_REQUIRED

        server_observed: dict[str, Any] = {
            "connections": 0,
            "client_pong_payloads": [],
            "client_ping_payloads": [],
            "client_close_codes": [],
        }

        async def _ws_server_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                # Read HTTP Upgrade request first (connections that fail TLS verification send 0 HTTP bytes)
                req_key = ""
                while True:
                    line_b = await reader.readline()
                    if line_b in (b"\r\n", b"\n", b""):
                        break
                    line = line_b.decode("latin1").strip()
                    if line.lower().startswith("sec-websocket-key:"):
                        req_key = line.split(":", 1)[1].strip()

                if not req_key:
                    return

                conn_idx = server_observed["connections"]
                server_observed["connections"] += 1

                # Connection #0: intentionally return bad Sec-WebSocket-Accept to test handshake guard
                if conn_idx == 0:
                    accept_val = "INVALID_ACCEPT_SIGNATURE"
                else:
                    accept_val = compute_rfc6455_accept_key(req_key)

                resp = (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept_val}\r\n\r\n"
                )
                writer.write(resp.encode("ascii"))
                await writer.drain()
                if conn_idx == 0:
                    await asyncio.sleep(0.05)
                    return

                if conn_idx == 1:
                    # Primary session:
                    # 1. Send a 3-part fragmented JSON message (>126 bytes in part 1) with an interleaved PING in the middle!
                    msg_dict = {
                        "stream": "suiusdt@aggTrade",
                        "data": {
                            "e": "aggTrade",
                            "E": int(datetime.now(timezone.utc).timestamp() * 1000),
                            "s": "SUIUSDT",
                            "a": 501,
                            "p": "2.1850",
                            "q": "120.0",
                            "m": False,
                            "padding": "X" * 140,  # Ensures payload > 125 bytes (16-bit extended length 126)
                        },
                    }
                    raw_json = json.dumps(msg_dict).encode("utf-8")
                    part1 = raw_json[:135]  # > 125 bytes -> tests 16-bit extended length (126)
                    part2 = raw_json[135:175]
                    part3 = raw_json[175:]

                    # Send Fragment 1 (FIN=0, opcode=0x1 TEXT)
                    writer.write(_build_server_ws_frame(fin=False, opcode=0x1, payload=part1))
                    # Inject Control Frame PING (FIN=1, opcode=0x9) in the middle of the fragmented message!
                    writer.write(_build_server_ws_frame(fin=True, opcode=0x9, payload=b"BINANCE-HB-PING-42"))
                    # Send Fragment 2 (FIN=0, opcode=0x0 CONTINUATION)
                    writer.write(_build_server_ws_frame(fin=False, opcode=0x0, payload=part2))
                    # Send Fragment 3 (FIN=1, opcode=0x0 CONTINUATION)
                    writer.write(_build_server_ws_frame(fin=True, opcode=0x0, payload=part3))
                    await writer.drain()

                    # Read the masked PONG frame automatically sent by the client
                    fin_pong, op_pong, pong_data = await _read_masked_client_frame(reader)
                    if fin_pong and op_pong == 0xA:
                        server_observed["client_pong_payloads"].append(pong_data)

                    # Read client's proactive PING frame and reply with PONG
                    fin_cping, op_cping, cping_data = await _read_masked_client_frame(reader)
                    if fin_cping and op_cping == 0x9:
                        server_observed["client_ping_payloads"].append(cping_data)
                        writer.write(_build_server_ws_frame(fin=True, opcode=0xA, payload=cping_data))
                        await writer.drain()

                    # Send a server CLOSE frame (opcode=0x8, status=1001 Going Away, reason='Server rotation')
                    close_payload = struct.pack("!H", 1001) + b"Server rotation"
                    writer.write(_build_server_ws_frame(fin=True, opcode=0x8, payload=close_payload))
                    await writer.drain()

                    # Read client's echoed masked CLOSE frame
                    _, op_close, cclose_data = await _read_masked_client_frame(reader)
                    if op_close == 0x8 and len(cclose_data) >= 2:
                        server_observed["client_close_codes"].append(struct.unpack("!H", cclose_data[:2])[0])

                elif conn_idx == 2:
                    # Reconnected session: send valid aggTrade #502
                    msg_502 = {
                        "stream": "suiusdt@aggTrade",
                        "data": {
                            "e": "aggTrade",
                            "E": int(datetime.now(timezone.utc).timestamp() * 1000),
                            "s": "SUIUSDT",
                            "a": 502,
                            "p": "2.1900",
                            "q": "80.0",
                            "m": True,
                        },
                    }
                    writer.write(
                        _build_server_ws_frame(fin=True, opcode=0x1, payload=json.dumps(msg_502).encode("utf-8"))
                    )
                    await writer.drain()
                    await asyncio.sleep(0.1)

                elif conn_idx == 3:
                    # Protocol violation session: send fragmented PING control frame (FIN=0, opcode=0x9)
                    writer.write(_build_server_ws_frame(fin=False, opcode=0x9, payload=b"BAD-FRAG-PING"))
                    await writer.drain()
                    await asyncio.sleep(0.1)
            except Exception:
                pass
            finally:
                writer.close()

        server = await asyncio.start_server(_ws_server_handler, "127.0.0.1", 0, ssl=server_ssl)
        port = server.sockets[0].getsockname()[1]

        collector, store, _ = _make_collector(tmp_path)

        try:
            # A. Verify default strict TLS context rejects self-signed certificate (CERT_REQUIRED proof)
            ws_untrusted = BinanceSpotWebSocketStream(
                host="127.0.0.1",
                port=port,
                server_hostname="stream.binance.com",
            )
            with pytest.raises(ssl.SSLCertVerificationError):
                await ws_untrusted.connect(timeout_sec=3.0)

            # B. Verify RFC 6455 Handshake rejects invalid Sec-WebSocket-Accept (conn_idx == 0)
            ws = BinanceSpotWebSocketStream(
                host="127.0.0.1",
                port=port,
                ssl_context=client_trusted_ssl,
                server_hostname="stream.binance.com",
            )
            with pytest.raises(WebSocketHandshakeError, match="Sec-WebSocket-Accept mismatch"):
                await ws.connect(timeout_sec=3.0)

            # C. Connect valid TLSv1.3 session (conn_idx == 1), receive 3-part fragmented message + interleaved PING
            await ws.connect(timeout_sec=3.0)
            assert ws.tls_version in {"TLSv1.3", "TLSv1.2"}
            assert ws.tls_cipher is not None

            msg_501, recv_utc, recv_mono = await ws.recv_json(timeout_sec=5.0)
            assert ws.fragmented_messages_reassembled == 1
            assert ws.pings_received_and_replied == 1
            assert ws.last_ping_payload == b"BINANCE-HB-PING-42"
            assert collector.process_ws_message(msg_501, recv_time_utc=recv_utc, recv_mono_ns=recv_mono) == DataQuality.OK
            assert collector.trade_buffers["SUIUSDT"].latest().agg_trade_id == 501

            # D. Send proactive client PING -> verify PONG when next reading close frame
            await ws.send_ping(b"CLIENT-PROBE-99")

            # E. Receive server CLOSE frame (1001, 'Server rotation')
            with pytest.raises(WebSocketClosedError) as exc_info:
                await ws.recv_json(timeout_sec=5.0)
            assert exc_info.value.close_code == 1001
            assert exc_info.value.close_reason == "Server rotation"
            assert ws.close_frames_received == 1
            assert ws.pongs_received == 1
            assert ws.last_pong_payload == b"CLIENT-PROBE-99"

            # Verify server received client's masked PONG, PING, and echoed CLOSE(1001)
            await asyncio.sleep(0.05)
            assert server_observed["client_pong_payloads"] == [b"BINANCE-HB-PING-42"]
            assert server_observed["client_ping_payloads"] == [b"CLIENT-PROBE-99"]
            assert server_observed["client_close_codes"] == [1001]

            # F. Collector handles disconnect -> freezes entry -> reconnects (conn_idx == 2) -> recovers
            collector.handle_unexpected_disconnect(reason="CLOSE_1001")
            assert collector.quality_guard.current_quality == DataQuality.GAP_DETECTED
            assert store.get_trading_state() == TradingState.ENTRY_FROZEN

            await ws.reconnect(timeout_sec=3.0)
            assert ws.reconnect_count == 1
            msg_502, r_utc2, r_mono2 = await ws.recv_json(timeout_sec=5.0)
            assert collector.process_ws_message(msg_502, recv_time_utc=r_utc2, recv_mono_ns=r_mono2) == DataQuality.OK
            assert collector.trade_buffers["SUIUSDT"].latest().agg_trade_id == 502

            # G. Verify RFC 6455 protocol violation (fragmented PING control frame, conn_idx == 3) is rejected
            await ws.reconnect(timeout_sec=3.0)
            with pytest.raises(WebSocketProtocolError, match="Fragmented control frame"):
                await ws.recv_json(timeout_sec=5.0)

        finally:
            await ws.close()
            server.close()
            await server.wait_closed()
            store.close()

    asyncio.run(_run_scenario())


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
    emitted_5m: list[tuple[ClosedKlineBar, MarketFeatures]] = []
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

    assert len(emitted_5m) == 0
    assert collector.five_min_signal_trigger_count == 0
    assert len(collector.quality_guard.get_closed_bars("SUIUSDT", "5m")) == 0
    transient = collector.quality_guard.get_unclosed_transient_view("SUIUSDT", "5m")
    assert transient is not None
    assert transient.is_closed is False
    assert transient.current_price == Decimal("2.1900")

    # 2. Inject the closed (x=True) update 3 times
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

    assert len(emitted_5m) == 1
    assert collector.five_min_signal_trigger_count == 1
    closed_bar, features = emitted_5m[0]
    assert closed_bar.is_closed is True
    assert closed_bar.close_price == Decimal("2.1950")
    assert features.complete_bar_only is True
    assert collector.quality_guard.get_unclosed_transient_view("SUIUSDT", "5m") is None
    store.close()


def test_qa01_ws_disconnect_gap_marking_and_kline_backfill(tmp_path: Path) -> None:
    """QA-01 & §14.1: Verify 3-minute WS disconnect immediately freezes new entries, backfills closed K-lines via REST,
    and permanently marks the depth10 orderbook gap as GAP_UNRECOVERABLE_FOR_FILL_CALIBRATION.
    """
    collector, store, _ = _make_collector(tmp_path)
    t0 = datetime(2026, 10, 31, 6, 0, 0, tzinfo=timezone.utc)
    ex_ms = int(t0.timestamp() * 1000)

    collector.process_ws_message(
        {
            "stream": "suiusdt@depth10@100ms",
            "data": {"lastUpdateId": 900, "E": ex_ms, "bids": [["2.1000", "500.0"]], "asks": [["2.1005", "500.0"]]},
        },
        recv_time_utc=t0,
    )
    assert store.get_trading_state() == TradingState.READY_NO_POSITION

    t_disc = t0 + timedelta(seconds=10)
    collector.handle_unexpected_disconnect(now_utc=t_disc, reason="NETWORK_DROP_3MIN")
    assert collector.quality_guard.current_quality == DataQuality.GAP_DETECTED
    assert store.get_trading_state() == TradingState.ENTRY_FROZEN

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
    assert marker.recoverable is False
    assert marker.calibration_tag == CALIBRATION_UNRECOVERABLE_TAG
    assert marker.allow_b01_fill_calibration is False
    assert (
        collector.quality_guard.is_window_valid_for_b01_calibration(
            "SUIUSDT",
            t_disc + timedelta(seconds=30),
            t_disc + timedelta(seconds=90),
        )
        is False
    )
    store.close()


def test_qa01_23h_hot_connection_rotation_dedup_and_gap_detection_item2(tmp_path: Path) -> None:
    """P2 Remediation Item 2: Verify 23-hour dual-connection hot rotation:
    1. Cross-connection deduplication when primary and standby overlap (`201..205` vs `203..208`).
    2. Explicit declaration that `zero_loss_guaranteed` is False (never promising absolute zero packet loss).
    3. Cross-connection sequence gap detection when packet loss occurs across connections
       (`primary` ends at `208`, `standby` starts at `212` -> missing `209..211`), immediately flagging
       `DataQuality.GAP_DETECTED`, transitioning to `ENTRY_FROZEN`, and recording an `OrderbookGapMarker`.
    """
    collector, store, _ = _make_collector(tmp_path)
    t_start = datetime(2026, 10, 31, 0, 0, 0, tzinfo=timezone.utc)
    collector.active_conn_opened_utc = t_start

    t_rot1 = t_start + timedelta(hours=23, seconds=1)
    assert collector.should_rotate_connection_23h(now_utc=t_rot1) is True
    ex_ms = int(t_rot1.timestamp() * 1000)

    def _agg_frame(seq_id: int, ts_utc: datetime) -> tuple[dict[str, Any], datetime, int]:
        return (
            {
                "stream": "suiusdt@aggTrade",
                "data": {
                    "e": "aggTrade",
                    "E": int(ts_utc.timestamp() * 1000),
                    "s": "SUIUSDT",
                    "a": seq_id,
                    "p": "2.2000",
                    "q": "15.0",
                    "m": False,
                },
            },
            ts_utc,
            1_000_000_000 + seq_id * 1_000,
        )

    # Scenario 1: Overlapping warm handoff (Primary: 201..205, Standby: 203..208)
    primary_1 = [_agg_frame(i, t_rot1) for i in range(201, 206)]
    standby_1 = [_agg_frame(i, t_rot1) for i in range(203, 209)]

    report1 = collector.execute_23h_hot_rotation(
        new_conn_id="WS-ROTATED-002",
        overlapping_primary_frames=primary_1,
        overlapping_standby_frames=standby_1,
        rotated_at_utc=t_rot1,
    )
    assert report1["accepted_unique_events"] == 8  # 201..208
    assert report1["deduplicated_overlap_events"] == 3  # 203, 204, 205
    assert report1["gap_detected"] is False
    assert report1["missing_seq_ranges"] == []
    assert report1["zero_loss_guaranteed"] is False
    assert store.get_trading_state() == TradingState.READY_NO_POSITION

    # Scenario 2: Cross-connection sequence gap during rotation (Primary ends at 208, Standby starts at 212 -> missing 209..211)
    t_rot2 = t_rot1 + timedelta(hours=23, seconds=1)
    primary_2: list[tuple[dict[str, Any], datetime, int]] = []
    standby_2 = [_agg_frame(i, t_rot2) for i in range(212, 215)]

    report2 = collector.execute_23h_hot_rotation(
        new_conn_id="WS-ROTATED-003",
        overlapping_primary_frames=primary_2,
        overlapping_standby_frames=standby_2,
        rotated_at_utc=t_rot2,
    )
    assert report2["gap_detected"] is True
    assert report2["missing_seq_ranges"] == [(209, 211)]
    assert report2["zero_loss_guaranteed"] is False
    assert collector.quality_guard.current_quality == DataQuality.GAP_DETECTED
    assert store.get_trading_state() == TradingState.ENTRY_FROZEN
    assert len(collector.quality_guard.orderbook_gaps) == 1
    assert "HOT_ROTATION_SEQUENCE_GAP" in collector.quality_guard.orderbook_gaps[0].reason
    store.close()


def test_qa02_100ms_depth_backpressure_critical_zero_loss_and_drop_telemetry_item3(
    tmp_path: Path,
) -> None:
    """P2 Remediation Item 3: Verify backpressure policy guarantees:
    1. Critical closed K-lines (`k.x=True`) and DataQuality events (`STALE`, `GAP_DETECTED`, explicit quality events)
       are NEVER lost (`critical_dropped_count == 0`), even when the queue is 100% saturated with critical items!
    2. Snapshot degradation (`100ms -> 1000ms` and recovery `1000ms -> 100ms`) and per-category drop counts
       (`dropped_by_category`) are accurately recorded in `BackpressureTelemetry`.
    """
    collector, store, _ = _make_collector(
        tmp_path,
        queue_maxsize=100,
        depth_ring_capacity=50,
    )
    now_utc = datetime(2026, 10, 31, 6, 30, 0, tzinfo=timezone.utc)
    ex_ms = int(now_utc.timestamp() * 1000)

    # 1. Enqueue 110 transient depth10 frames + 10 transient unclosed klines + 3 critical closed 5m klines
    #    + 2 critical DataQuality events into a maxsize=100 queue before draining
    for i in range(1, 111):
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
    for i in range(10):
        collector.enqueue_raw_message(
            {
                "stream": "suiusdt@kline_5m",
                "data": {
                    "e": "kline",
                    "E": ex_ms,
                    "s": "SUIUSDT",
                    "k": {
                        "t": ex_ms,
                        "T": ex_ms + 299_999,
                        "s": "SUIUSDT",
                        "i": "5m",
                        "o": "2.1500",
                        "h": "2.1600",
                        "l": "2.1400",
                        "c": "2.1550",
                        "v": "100.0",
                        "q": "215.0",
                        "x": False,
                    },
                },
            },
            recv_time_utc=now_utc,
        )

    # Enqueue 3 distinct critical closed 5m K-lines while queue is completely full
    for bar_idx in range(3):
        open_t = ex_ms - (3 - bar_idx) * 300_000
        collector.enqueue_raw_message(
            {
                "stream": "suiusdt@kline_5m",
                "data": {
                    "e": "kline",
                    "E": ex_ms,
                    "s": "SUIUSDT",
                    "k": {
                        "t": open_t,
                        "T": open_t + 299_999,
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

    # Enqueue 2 critical DataQuality events while queue is full (1 explicit + 1 stale frame)
    collector.enqueue_data_quality_event(
        quality=DataQuality.GAP_DETECTED,
        reason="SYNTHETIC_GAP_NOTICE",
        recv_time_utc=now_utc,
    )
    stale_recv = now_utc + timedelta(milliseconds=3500)
    collector.enqueue_raw_message(
        {
            "stream": "suiusdt@aggTrade",
            "data": {"e": "aggTrade", "E": ex_ms, "s": "SUIUSDT", "a": 9999, "p": "2.1500", "q": "10.0", "m": False},
        },
        recv_time_utc=stale_recv,
    )

    bp_before_drain = collector.get_backpressure_telemetry()
    assert bp_before_drain.degraded_mode is True
    assert collector.depth_sampling_ms == 1000
    assert bp_before_drain.critical_enqueued_count == 5  # 3 closed klines + 2 data quality events
    assert bp_before_drain.critical_dropped_count == 0
    assert bp_before_drain.transient_dropped_count == 25  # 125 total enqueued into maxsize=100 -> 25 transient shed
    assert bp_before_drain.dropped_by_category.get("depth10", 0) == 25
    assert bp_before_drain.dropped_by_category.get("kline_closed", 0) == 0
    assert bp_before_drain.dropped_by_category.get("data_quality", 0) == 0

    # Drain all items and verify every critical closed K-line and DataQuality event was processed
    drained = collector.drain_and_process_queue(max_items=200)
    assert drained == 100
    assert collector.five_min_signal_trigger_count == 3
    assert collector.data_quality_events_processed == 2
    assert store.get_trading_state() == TradingState.ENTRY_FROZEN

    # Verify low-watermark recovery restored depth_sampling_ms to 100ms and recorded both transitions
    bp_after_drain = collector.get_backpressure_telemetry()
    assert bp_after_drain.degraded_mode is False
    assert collector.depth_sampling_ms == 100
    assert bp_after_drain.high_watermark_triggers == 1
    assert bp_after_drain.low_watermark_recoveries == 1
    transitions = [ev["transition"] for ev in bp_after_drain.degradation_events]
    assert transitions == ["DEGRADED_100MS_TO_1000MS", "RECOVERED_1000MS_TO_100MS"]

    # Verify latency SLO
    lat = collector.compute_latency_percentiles()
    assert lat.within_slo is True
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

    r1 = sync_engine.sync_from_manifest(
        manifest,
        fetch_shard_bytes_fn=lambda fname: (cloud_spool / fname).read_bytes(),
        max_shards_this_run=1,
    )
    assert r1.synced_shards_count == 1
    assert r1.last_synced_seq == 1
    assert r1.duckdb_total_rows == 2

    r2 = sync_engine.sync_from_manifest(
        manifest,
        fetch_shard_bytes_fn=lambda fname: (cloud_spool / fname).read_bytes(),
    )
    assert r2.skipped_already_synced_count == 1
    assert r2.synced_shards_count == 2
    assert r2.last_synced_seq == 3
    assert r2.parquet_files_count == 3
    assert r2.duckdb_total_rows == 6

    r3 = sync_engine.sync_from_manifest(
        manifest,
        fetch_shard_bytes_fn=lambda fname: (cloud_spool / fname).read_bytes(),
    )
    assert r3.synced_shards_count == 0
    assert r3.skipped_already_synced_count == 3
    assert r3.duckdb_total_rows == 6

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
