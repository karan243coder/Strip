# -*- coding: utf-8 -*-
"""Stripchat Live Recorder — standalone Telegram bot (no BIMBO features)."""
from __future__ import annotations

import os
import gc
import sys
import time
import logging
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except Exception:
    pass

import config  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logger = logging.getLogger("sc.bot")


def _excepthook(typ, val, tb):
    logger.error("unhandled", exc_info=(typ, val, tb))


sys.excepthook = _excepthook


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK stripchat-live-bot")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


def run_health():
    try:
        httpd = HTTPServer(("0.0.0.0", config.PORT), HealthHandler)
        logger.info("health on :%s", config.PORT)
        httpd.serve_forever()
    except Exception as e:
        logger.warning("health server: %s", e)


def _cleanup_loop():
    hours = config.AUTO_CLEANUP_HOURS
    if hours <= 0:
        return
    threshold = hours * 3600
    while True:
        time.sleep(1800)
        now = time.time()
        root = config.DOWNLOAD_DIR
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            for name in filenames:
                p = os.path.join(dirpath, name)
                try:
                    if now - os.path.getmtime(p) > threshold:
                        os.remove(p)
                except Exception:
                    pass
            for name in dirnames:
                p = os.path.join(dirpath, name)
                try:
                    if not os.listdir(p):
                        os.rmdir(p)
                except Exception:
                    pass
        try:
            gc.collect()
        except Exception:
            pass


async def _monitor_forever(app):
    import monitor as _mon
    _mon.load()
    while True:
        try:
            _mon._LOOP_STARTED = False
            await _mon.run_loop(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("monitor loop died — restart 3s")
        try:
            gc.collect()
        except Exception:
            pass
        await asyncio.sleep(3)


def main():
    if not config.BOT_TOKEN or not config.API_ID or not config.API_HASH:
        logger.critical(
            "Set SC_BOT_TOKEN (or BOT_TOKEN), SC_API_ID (or API_ID), "
            "SC_API_HASH (or API_HASH). BIMBO token mat reuse karo — alag bot banao."
        )
        sys.exit(1)
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    threading.Thread(target=_cleanup_loop, daemon=True).start()

    from pyrogram import Client, idle
    import handlers

    app = Client(
        name=config.SESSION_NAME,
        bot_token=config.BOT_TOKEN,
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        workers=config.WORKERS,
        workdir=ROOT,
        max_concurrent_transmissions=2,
        sleep_threshold=60,
    )
    handlers.register(app)
    from engine import load_key_map
    km = load_key_map()
    logger.info(
        "starting | owner=%s allow_all=%s keys=%s admins=%s",
        config.OWNER_ID, config.ALLOW_ALL, len(km), len(config.ADMIN_IDS),
    )
    try:
        loop = asyncio.get_event_loop()

        def _aio_err(loop, context):
            logger.error("asyncio: %s", context.get("message"), exc_info=context.get("exception"))

        try:
            loop.set_exception_handler(_aio_err)
        except Exception:
            pass
    except Exception:
        pass

    app.start()
    me = app.get_me()
    logger.info("telegram ok @%s id=%s", me.username, me.id)
    try:
        asyncio.get_event_loop().create_task(_monitor_forever(app))
        logger.info("auto-monitor supervisor scheduled")
    except Exception as e:
        logger.warning("monitor loop not started: %s", e)
    threading.Thread(target=run_health, daemon=True).start()
    try:
        idle()
    except KeyboardInterrupt:
        logger.info("stop requested")
    except Exception:
        logger.exception("idle crash — restart process (Koyeb)")
        raise
    try:
        app.stop()
    except Exception:
        pass


if __name__ == "__main__":
    main()
