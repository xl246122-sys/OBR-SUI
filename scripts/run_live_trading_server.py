"""Launcher for SUI-Quant Live Market PAPER/LIVE Runtime Server on 8765."""

from __future__ import annotations

import argparse
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sui_quant.daemon.live_paper_runtime_server import (
    LivePaperRuntimeEngine,
    create_http_handler,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    host = args.host
    port = args.port
    runtime_data_dir = ROOT_DIR / "data" / "runtime_live_paper"

    engine = LivePaperRuntimeEngine(data_dir=runtime_data_dir, reset_to_paper_on_boot=True)
    engine.fetch_binance_market_snapshot()

    stop_event = threading.Event()

    def market_poll_loop() -> None:
        while not stop_event.is_set():
            engine.fetch_binance_market_snapshot()
            stop_event.wait(2.0)

    poll_thread = threading.Thread(target=market_poll_loop, daemon=True, name="BinanceLiveFeed")
    poll_thread.start()

    handler_cls = create_http_handler(engine)
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((host, port), handler_cls)

    print("==================================================================", flush=True)
    print(" SUI-Quant Real-Time PAPER/LIVE Runtime Server Started!", flush=True)
    print(f" Listening on  : http://{host}:{port}", flush=True)
    print(f" Default Mode  : {engine.execution_mode.value} (LIVE Switch: OFF)", flush=True)
    print(f" Initial SUI   : {engine.sui_last_price} USDT | BTC: {engine.btc_last_price} USDT", flush=True)
    print("==================================================================", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
