# -*- coding: utf-8 -*-
"""Stripchat Live Recorder — standalone Telegram bot (no BIMBO features)."""
from __future__ import annotations

import os
import sys
import time
import logging
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
        max_concurrent_transmissions=4,
        sleep_threshold=30,
    )
    handlers.register(app)
    from engine import load_key_map
    km = load_key_map()
    logger.info(
        "starting | owner=%s allow_all=%s keys=%s admins=%s",
        config.OWNER_ID, config.ALLOW_ALL, len(km), len(config.ADMIN_IDS),
    )
    app.start()
    me = app.get_me()
    logger.info("telegram ok @%s id=%s", me.username, me.id)
    # health tabhi — crash pe Koyeb ko false-healthy na lage
    threading.Thread(target=run_health, daemon=True).start()
    idle()
    app.stop()


if __name__ == "__main__":
    main()
