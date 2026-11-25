import argparse
import asyncio
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
import threading
import time

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse

from .config import Config
from .engine import Engine
from .demo import Demo
from .ibkr import IBKRAdapter


class Inbox:
    def __init__(self, capacity):
        self.queue = deque()
        self.capacity = capacity
        self.dropped = 0
        self.lock = threading.Lock()

    def put(self, event):
        with self.lock:
            if len(self.queue) >= self.capacity:
                self.queue.popleft()
                self.dropped += 1
            self.queue.append(event)

    def drain(self):
        with self.lock:
            events = list(self.queue)
            self.queue.clear()
            dropped, self.dropped = self.dropped, 0
            return events, dropped


def create_app(config=None):
    cfg = config or Config.from_env()
    cfg.validate()
    if cfg.source == "nightwatch":
        from .nightwatch import create_nightwatch_app
        return create_nightwatch_app(cfg)
    engine = Engine(cfg)
    inbox = Inbox(cfg.queue_size)
    adapter = IBKRAdapter(cfg, inbox.put, engine.calendar) if cfg.source == "ibkr" else None
    demo = Demo(engine) if cfg.source == "demo" else None

    async def worker():
        last_demo = time.monotonic()
        while True:
            if demo:
                if time.monotonic()-last_demo >= 1:
                    demo.advance()
                    last_demo = time.monotonic()
            else:
                events, dropped = inbox.drain()
                now = time.time()
                if dropped:
                    engine.data_gap(dropped, now)
                for event in events:
                    normalized = adapter.dispatch(event) if event["kind"].startswith("control_") else [event]
                    for item in normalized:
                        engine.handle(item)
                engine.now = now
                engine.roll_day(now)
                if engine.connected and engine.spot and engine.chain_date == engine.day:
                    removed, added, _ = engine.window.update(engine.spot, engine.strikes, now)
                    for key in removed:
                        adapter.command("unsubscribe", key)
                    for key in added:
                        adapter.command("subscribe", key)
            await asyncio.sleep(.1)

    @asynccontextmanager
    async def lifespan(app):
        if demo:
            demo.bootstrap()
        if adapter:
            adapter.start()
        task = asyncio.create_task(worker())
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if adapter:
            await asyncio.to_thread(adapter.stop)

    app = FastAPI(title="SPX Tail Observer — read only", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine, app.state.adapter, app.state.inbox = engine, adapter, inbox

    @app.get("/")
    async def index():
        return FileResponse(Path(__file__).parent/"static"/"index.html")

    @app.get("/api/snapshot")
    async def snapshot(window: int = Query(300, ge=0, le=900), sort: str = "premium"):
        if window not in {0, 60, 300, 900}:
            window = 300
        return engine.snapshot(demo.clock if demo else time.time(), window, sort)

    @app.get("/api/debug")
    async def debug():
        engine.snapshot(demo.clock if demo else time.time())
        return {"source": cfg.source, "demo": cfg.source == "demo", "series": list(engine.debug_history),
                "gex_snapshots": list(engine.gex_history)}

    @app.get("/health")
    async def health():
        return {"service": "up", "mode": cfg.source, "read_only": True,
                "broker_connected": engine.connected if adapter else False}

    return app


def main():
    cfg = Config.from_env()
    parser = argparse.ArgumentParser(description="SPXW read-only tail observation dashboard")
    parser.add_argument("--source", choices=["demo", "ibkr", "nightwatch"], default=cfg.source)
    parser.add_argument("--port", type=int, default=cfg.port)
    parser.add_argument("--ib-port", type=int, default=cfg.ib_port)
    parser.add_argument("--ib-host", default=cfg.ib_host)
    parser.add_argument("--ib-client-id", type=int, default=cfg.ib_client_id)
    parser.add_argument("--massive-spot", action="store_true", help="Enable independent SPX price and prompt for its key")
    args = parser.parse_args()
    cfg.source, cfg.port = args.source, args.port
    cfg.ib_port, cfg.ib_host, cfg.ib_client_id = args.ib_port, args.ib_host, args.ib_client_id
    if cfg.source == "nightwatch" and not cfg.nightwatch_key:
        import getpass
        cfg.nightwatch_key = getpass.getpass("Nightwatch API key (hidden, not saved): ").strip()
        if not cfg.nightwatch_key:
            parser.error("API key is empty; nothing was connected")
    if cfg.source == "nightwatch" and args.massive_spot and not cfg.massive_spot_key:
        import getpass
        cfg.massive_spot_key = getpass.getpass("Massive SPX API key (hidden; blank to skip): ").strip()
    import uvicorn
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, access_log=False, workers=1)


if __name__ == "__main__":
    main()
